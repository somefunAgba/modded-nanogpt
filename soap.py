from itertools import chain
import torch

class SOAP(torch.optim.Optimizer):
    """
    Implements SOAP algorithm (https://arxiv.org/abs/2409.11321).

    Parameters:
        params (`Iterable[nn.parameter.Parameter]`):
            Iterable of parameters to optimize or dictionaries defining parameter groups.
        lr (`float`, *optional*, defaults to 0.003):
            The learning rate to use.
        betas (`Tuple[float,float]`, *optional*, defaults to `(0.95, 0.95)`):
            Adam's betas parameters (b1, b2).
        shampoo_beta (`float`, *optional*, defaults to -1):
            If >= 0, use this beta for the preconditioner (L and R in paper, state['GG'] below) moving average instead of betas[1].
        eps (`float`, *optional*, defaults to 1e-08):
            Adam's epsilon for numerical stability.
        precondition_frequency (`int`, *optional*, defaults to 10):
            How often to update the preconditioner.
    """

    def __init__(
        self,
        params,
        lr: float = 3e-3,
        betas=(0.95, 0.95),
        shampoo_beta: float= -1,
        eps: float = 1e-8,
        precondition_frequency: int=10,
    ):
        defaults = {
            "lr": lr,
            "betas": betas,
            "shampoo_beta": shampoo_beta,
            "eps": eps,
            "precondition_frequency": precondition_frequency,
        }
        super().__init__(params, defaults)
        
    @torch.no_grad()
    def step(self):
        """
        Performs a single optimization step.
        """
        for group in self.param_groups:

            beta1, beta2 = group["betas"]
            for p in group["params"]:
                if p.grad is None:
                    continue
                grad = p.grad

                state = self.state[p]
                
                if "step" not in state:
                    state["step"] = 0 
                    
                # State initialization
                if "exp_avg" not in state:
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(grad)
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(grad)
                
                if 'Q' not in state:
                    self.init_preconditioner(
                        grad,
                        state,
                        precondition_frequency=group['precondition_frequency'],
                        shampoo_beta=(group['shampoo_beta'] if group['shampoo_beta'] >= 0 else group["betas"][1]),
                    )
                    self.update_preconditioner(grad, state)
                    continue # first step is skipped so that we never use the current gradients in the projection.

                # Projecting gradients to the eigenbases of Shampoo's preconditioner 
                # i.e. projecting to the eigenbases of matrices in state['GG']
                grad_projected = self.project(grad, state)

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]

                state["step"] += 1

                # Decay the first and second moment running average coefficient
                # In-place operations to update the averages at the same time
                exp_avg.lerp_(grad, 1-beta1)
                exp_avg_sq.lerp_(grad_projected.square(), 1-beta2)

                # Projecting the exponential moving average of gradients to the eigenbases of Shampoo's preconditioner 
                # i.e. projecting to the eigenbases of matrices in state['GG']
                exp_avg_projected = self.project(exp_avg, state)
                
                step_size = group["lr"]
                bias_correction1 = 1.0 - beta1 ** (state["step"])
                bias_correction2 = 1.0 - beta2 ** (state["step"])
                step_size = step_size * (bias_correction2 ** .5) / bias_correction1

                # Projecting back the preconditioned (by Adam) exponential moving average of gradients
                # to the original space
                denom = exp_avg_sq.sqrt().add_(group["eps"])
                norm_grad = self.project_back(exp_avg_projected / denom, state)

                p.add_(norm_grad, alpha=-step_size)

                # Update is done after the gradient step to avoid using current gradients in the projection.
                self.update_preconditioner(grad, state)
    
    def init_preconditioner(self, grad, state, precondition_frequency=10, 
                            shampoo_beta=0.95):
        """
        Initializes the preconditioner matrices (L and R in the paper).
        """
        state['GG'] = [torch.zeros(d, d, device=grad.device) for d in grad.shape] # Will hold all the preconditioner matrices (L and R in the paper).
        state['Q'] = None # Will hold all the eigenbases of the preconditioner.
        state['precondition_frequency'] = precondition_frequency
        state['shampoo_beta'] = shampoo_beta

    def project(self, grad, state):
        """
        Projects the gradient to the eigenbases of the preconditioner.
        """
        for mat in state['Q']:
            grad = torch.tensordot(grad, mat, dims=[[0], [0]])
        return grad
        
    def update_preconditioner(self, grad, state):
        """
        Updates the preconditioner matrices and the eigenbases (L, R, Q_L, Q_R in the paper).
        """
        for idx, sh in enumerate(grad.shape):
            # Contracts across all dimensions except for k.
            outer_product = torch.tensordot(grad, grad, dims=[[*chain(range(idx), range(idx + 1, len(grad.shape)))]] * 2)
            state['GG'][idx].lerp_(outer_product, 1-state['shampoo_beta'])
        if state['Q'] is None:
            state['Q'] = self.get_orthogonal_matrix(state['GG'])
        if state['step'] > 0 and state['step'] % state['precondition_frequency'] == 0:
            state['Q'] = self.get_orthogonal_matrix_QR(state)

    def project_back(self, grad, state):
        """
        Projects the gradient back to the original space.
        """
        for mat in state['Q']:
            grad = torch.tensordot(grad, mat, dims=[[0], [1]])
        return grad

    def get_orthogonal_matrix(self, mat):
        """
        Computes the eigenbases of the preconditioner using torch.linalg.eigh decomposition.
        """
        final = []
        for m in mat:
            #try:
            #    _, Q = torch.linalg.eigh(m+1e-30*torch.eye(m.shape[0], device=m.device))
            #except:
            #    _, Q = torch.linalg.eigh(m.to(torch.float64)+1e-30*torch.eye(m.shape[0], device=m.device))
            #    Q = Q.to(m.dtype)
            _, Q = torch.linalg.eigh(m+1e-30*torch.eye(m.shape[0], device=m.device))
            Q = torch.flip(Q, [1])
            final.append(Q)
        return final

    def get_orthogonal_matrix_QR(self, state):
        """
        Computes the eigenbases of the preconditioner using one round of power iteration 
        followed by torch.linalg.qr decomposition.
        """
        precond_list = state['GG']
        orth_list = state['Q']

        exp_avg_sq = state['exp_avg_sq']

        final = []
        for ind, (m, o) in enumerate(zip(precond_list, orth_list)):
            est_eig = torch.diag(o.T @ m @ o)
            sort_idx = torch.argsort(est_eig, descending=True)
            exp_avg_sq = exp_avg_sq.index_select(ind, sort_idx)
            o = o[:, sort_idx]
            power_iter = m @ o
            Q, _ = torch.linalg.qr(power_iter)
            final.append(Q)

        state['exp_avg_sq'] = exp_avg_sq
        return final
