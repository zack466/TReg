"""
This module includes LDM-based inverse problem solvers.
Forward operators follow DPS and DDRM/DDNM.
"""

from typing import Any, Callable, Dict, Optional

import torch
from diffusers import DDIMScheduler, StableDiffusionPipeline
from torchvision.utils import save_image
from tqdm import tqdm

from functions.conjugate_gradient import CG
from functions.svd_operators import A_functions as A_func
from ldm.modules.encoders.modules import FrozenClipImageEmbedder

####### Factory #######
__SOLVER__ = {}

def register_solver(name: str):
    def wrapper(cls):
        if __SOLVER__.get(name, None) is not None:
            raise ValueError(f"Solver {name} already registered.")
        __SOLVER__[name] = cls
        return cls
    return wrapper

def get_solver(name: str, **kwargs):
    if name not in __SOLVER__:
        raise ValueError(f"Solver {name} does not exist.")
    return __SOLVER__[name](**kwargs)

########################


@register_solver("ddim")
class UncondSolver():
    """
    Unconditional solver (i.e. Stble-Diffusion)
    This will generate samples without considering measurements.
    To define LDM functions for solvers.
    """
    def __init__(self,
                 solver_config: Dict,
                 model_key:str="runwayml/stable-diffusion-v1-5",
                 device: Optional[torch.device]=None,
                 **kwargs):
        self.device = device

        # TODO: can we use float16?
        pipe_dtype = torch.float16
        pipe = StableDiffusionPipeline.from_pretrained(model_key, torch_dtype=pipe_dtype).to(device)
        self.vae = pipe.vae
        self.tokenizer = pipe.tokenizer
        self.text_encoder = pipe.text_encoder
        self.unet = pipe.unet

        self.scheduler = DDIMScheduler.from_pretrained(model_key, subfolder="scheduler")
        total_timesteps = len(self.scheduler.timesteps)
        self.scheduler.set_timesteps(solver_config.num_sampling, device=device)
        self.skip = total_timesteps // solver_config.num_sampling

        self.final_alpha_cumprod = self.scheduler.final_alpha_cumprod.to(device)
        self.scheduler.alphas_cumprod = torch.cat([torch.tensor([1.0]), self.scheduler.alphas_cumprod])

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.sample(*args, **kwargs)

    # DDIM inversion
    @torch.no_grad()
    def inversion(self,
                  z0: torch.Tensor,
                  uc: torch.Tensor,
                  c: torch.Tensor,
                  cfg_guidance: float=1.0,
                  record_all: bool=False):
        """
        DDIM inversion (Hertz et al., 2022, "prompt-to-prompt")

        Args:
            z0 (torch.Tensor): encoded image latent
            uc (torch.Tensor): embedded null text
            c (torch.Tensor): embedded contional text
            cfg_guidance (float): CFG scale
            record_all (bool): if True, return list of latents at all time steps

        Returns:
            (torch.Tensor, None): inversed latent (zT) or list of latents {zt}
            
        """
        # initialize z_0
        zt = z0.clone().to(self.device)

        z_record = [zt.clone()]
         
        # loop
        pbar = tqdm(reversed(self.scheduler.timesteps), desc='DDIM Inversion')
        for t in pbar:
            prev_t = t - self.skip
            at = self.scheduler.alphas_cumprod[t]
            at_prev = self.scheduler.alphas_cumprod[prev_t] if prev_t >= 0 else self.final_alpha_cumprod

            noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance) 
            z0t = (zt - (1-at_prev).sqrt() * noise_pred) / at_prev.sqrt()
            zt = at.sqrt() * z0t + (1-at).sqrt() * noise_pred

            if record_all:
                z_record.append(zt)
        
        if record_all:
            zt = z_record

        return zt
    
    def initialize_latent(self,
                          inversion: bool=False,
                          src_img: Optional[torch.Tensor]=None,
                          **kwargs):
        if inversion:
            z = self.inversion(self.encode(src_img),
                               kwargs.get('uc'),
                               kwargs.get('c'),
                               cfg_guidance=1.0,
                               record_all=kwargs.get('record_all', False))
        else:
            z = torch.randn((1, 4, 64, 64)).to(self.device)
        return z.requires_grad_()

    def predict_noise(self,
                      zt: torch.Tensor,
                      t: torch.Tensor,
                      uc: torch.Tensor,
                      c: torch.Tensor,
                      cfg_guidance: float,
                      split: bool=False):
        """
        compuate epsilon_theta with CFG.
        args:
            zt (torch.Tensor): latent features
            t (torch.Tensor): timestep
            uc (torch.Tensor): null-text embedding
            c (torch.Tensor): text embedding
            cfg_guidance (float): CFG value
        """
        if uc is None or cfg_guidance == 1.0:
            t_in = t.unsqueeze(0)
            noise_pred = self.unet(zt, t_in, encoder_hidden_states=uc)['sample']
        else:
            c_embed = torch.cat([uc, c], dim=0)
            z_in = torch.cat([zt] * 2) 
            t_in = torch.cat([t.unsqueeze(0)] * 2)
            noise_pred = self.unet(z_in, t_in, encoder_hidden_states=c_embed)['sample']
            noise_uc, noise_c = noise_pred.chunk(2)
            noise_pred = noise_uc + cfg_guidance * (noise_c - noise_uc)

        if split:
            return noise_uc, noise_c
        else:
            return noise_pred

    @torch.no_grad()
    def get_text_embed(self, null_prompt, prompt):
        """
        Get text embedding.
        args:
            null_prompt (str): null text
            prompt (str): guidance text
        """
        # null text embedding (negation)
        null_text_input = self.tokenizer(null_prompt,
                                         padding='max_length',
                                         max_length=self.tokenizer.model_max_length,
                                         return_tensors="pt",)
        null_text_embed = self.text_encoder(null_text_input.input_ids.to(self.device))[0]

        # text embedding (guidance)
        text_input = self.tokenizer(prompt,
                                    padding='max_length',
                                    max_length=self.tokenizer.model_max_length,
                                    return_tensors="pt",
                                    truncation=True)
        text_embed = self.text_encoder(text_input.input_ids.to(self.device))[0]

        return null_text_embed, text_embed

    def encode(self, x):
        """
        xt -> zt
        """
        return self.vae.encode(x).latent_dist.sample() * 0.18215

    def decode(self, zt):
        """
        zt -> xt
        """
        zt = 1/0.18215 * zt
        img = self.vae.decode(zt).sample.float()
        return img

    @torch.autocast(device_type='cuda', dtype=torch.float16) 
    def sample(self,
               cfg_guidance=7.5,
               prompt=["",""],
               **kwargs):
        """
        Main function that defines each solver.
        This will generate samples without considering measurements.
        """
        
        # Text embedding
        uc, c = self.get_text_embed(null_prompt=prompt[0], prompt=prompt[1])

        # Initialize zT
        latent_dim = kwargs.get('latent_dim', 64)
        zt = torch.randn((1, 4, latent_dim, latent_dim)).to(self.device)
        zt = zt.requires_grad_()

        # Sampling
        pbar = tqdm(self.scheduler.timesteps, desc="SD")
        for _, t in enumerate(pbar):
            prev_t = t - self.skip
            at = self.scheduler.alphas_cumprod[t]
            at_prev = self.scheduler.alphas_cumprod[prev_t] if prev_t >= 0 else self.final_alpha_cumprod

            with torch.no_grad():
                noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance)
            
            # tweedie
            z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()

            # add noise
            zt = at_prev.sqrt() * z0t + (1-at_prev).sqrt() * noise_pred
        
        # for the last step, do not add noise
        img = self.decode(z0t)
        img = (img / 2 + 0.5).clamp(0, 1)
        return img.detach().cpu()

@register_solver(name='treg')
class TRegSolver(UncondSolver):
    def __init__(self,
                 solver_config: Dict,
                 model_key:str="runwayml/stable-diffusion-v1-5",
                 device: Optional[torch.device]=None,
                 **kwargs):
        super().__init__(solver_config, model_key, device, **kwargs)
        
        self.clip_img_enc = FrozenClipImageEmbedder(model='ViT-L/14',
                                                    device=device)
    
    @torch.no_grad()
    def data_consistency(self,
                         z0t: torch.Tensor,
                         measurement: torch.Tensor,
                         A: Callable,
                         At: Callable,
                         cg_lamb: float=1e-4):
        """
        Apply data consistency update via conjugate gradient.
        args:
            z0t (torch.Tensor): denoised estimate
            measurement (torch.Tensor): measurement
            A (Callable): forward operator
            At (Callable): adjoint operator
        """
        x0t = self.decode(z0t).detach()
        bvec = At(measurement) + cg_lamb * x0t.reshape(1, -1)
        x0y = CG(A=lambda x: At(A(x)) + cg_lamb * x,
                      b=bvec,
                      x=x0t,
                      m=5)
        z0y = self.encode(x0y)
        return z0y, x0y
    
    def adaptive_negation(self,
                          x0: torch.Tensor,
                          uc: torch.Tensor,
                          lr: float=1e-3,
                          num_iter: int=10):
        """
        Update null-text embedding to minimize the similarity in CLIP space.
        args:
            x0 (torch.Tensor): input image
            uc (torch.Tensor): null-text embedding
            lr (float): learning rate
            num_iter (int): number of iterations
        """
        uc = uc.detach()
        x0 = x0.detach()
        img_feats = self.clip_img_enc(x0).detach()
        img_feats = img_feats / img_feats.norm(dim=1, keepdim=True) # normalize

        uc.requires_grad = True
        optim = torch.optim.Adam([uc], lr=lr)

        for _ in range(num_iter):
            optim.zero_grad()
            sim = img_feats @ uc.permute(0,2,1)
            loss = sim.mean()
            loss.backward(retain_graph=True)
            optim.step()
        
        return uc
    
    @torch.autocast(device_type='cuda', dtype=torch.float16) 
    def sample(self,
               measurement: torch.Tensor,
               operator: A_func,
               cfg_guidance: float=7.5,
               prompt: list[str]=["", ""],
               **kwargs):
        """
        Solve inverse problem with TReg.
        """

        use_DPS = kwargs.get('use_DPS', False)
        use_AN = kwargs.get('use_AN', True)
        cg_lamb = kwargs.get('cg_lamb', 1e-4)
        null_lr = kwargs.get('null_lr', 1e-3)
        
        A = operator.A
        At = operator.At

        # Text embedding
        uc, c = self.get_text_embed(null_prompt=prompt[0], prompt=prompt[1])
        
        # Initialize zT
        # if record_all = True, zt is a list of latents at all time steps
        # in that case, zt[-1] is the final latent (zT)
        x_src = At(measurement).reshape(1, 3, 512, 512)
        zt = self.initialize_latent(inversion=False,
                                    src_img=x_src,
                                    uc=uc,
                                    c=c,
                                    record_all=False)

        # Sampling
        pbar = tqdm(self.scheduler.timesteps, desc="TReg")
        for step, t in enumerate(pbar):
            prev_t = t - self.skip
            at = self.scheduler.alphas_cumprod[t]
            at_prev = self.scheduler.alphas_cumprod[prev_t] if prev_t >= 0 else self.final_alpha_cumprod

            if step % 3 == 0 and step < 170:
                with torch.no_grad():
                    noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance)
                z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()

                # Data consistency update
                z0y, x0y = self.data_consistency(z0t, measurement, A, At, cg_lamb)

                # adaptive negation
                if use_AN:
                    uc = self.adaptive_negation(x0y, uc, lr=null_lr)

                # DDPM-like noise adding
                noise = torch.randn_like(z0y).to(self.device)
                z0_ema = at_prev * z0y + (1-at_prev) * z0t
                zt = at_prev.sqrt() * z0_ema + (1-at_prev) * noise_pred
                zt = zt + (1-at_prev).sqrt() * at_prev.sqrt() * noise
            
            else:
                if use_DPS:
                    dps_lamb = kwargs.get('dps_lamb')
                    dps_lamb = dps_lamb if dps_lamb is not None else at_prev.sqrt()

                    noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance=0)
                    z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()
                    zt_prime = at_prev.sqrt() * z0t + (1-at_prev).sqrt() * noise_pred

                    x0t = self.decode(z0t)
                    residue = torch.linalg.norm((measurement - A(x0t)).reshape(-1))
                    grad = torch.autograd.grad(residue, zt)[0]

                    zt = zt_prime - dps_lamb * grad
                else:
                    with torch.no_grad():
                        noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance)
                    z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()
                    zt = at_prev.sqrt() * z0t + (1-at_prev).sqrt() * noise_pred

        # for the last step, do not add noise
        img = self.decode(z0t)
        img = (img / 2 + 0.5).clamp(0, 1)
        return img.detach().cpu()


@register_solver(name='psld')
class LDPSSolver(TRegSolver):
    def __init__(self,
                 solver_config: Dict,
                 model_key:str="botp/stable-diffusion-v1-5",
                 device: Optional[torch.device]=None,
                 **kwargs):
        super().__init__(solver_config, model_key, device, **kwargs)

    @torch.autocast(device_type='cuda', dtype=torch.float16)
    def sample(self,
               measurement,
               operator,
               cfg_guidance,
               prompt=["",""],
               **kwargs):

        A = operator.A
        At = operator.At
        eta = 0.0  # but,, 1.0 is better than 0.0.

        uc, c = self.get_text_embed(null_prompt=prompt[0], prompt=prompt[1])

        # initialize z_T
        zt = torch.randn((1,4,64,64)).to(self.device)
        zt.requires_grad = True

        # loop
        pbar = tqdm(self.scheduler.timesteps, desc='PSLD')
        for step, t in enumerate(pbar):
            prev_t = t - self.skip
            at = self.scheduler.alphas_cumprod[t]
            at_prev = self.scheduler.alphas_cumprod[prev_t] if prev_t >= 0 else self.final_alpha_cumprod
            
            with torch.enable_grad():
                noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance)
                z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()

                # Equation (12) of DDIM
                noise = torch.randn_like(z0t).to(self.device)
                sig = eta * ((1-at_prev)/(1-at)).sqrt() * (1-at/at_prev).sqrt()
                zt_prime = at_prev.sqrt() * z0t + (1-at_prev-sig**2).sqrt() * noise_pred + sig * noise

                # data consistency
                x0t = self.decode(z0t)

                # uc = self.adaptive_negation(x0t, uc)

                residue = torch.linalg.norm((measurement - A(x0t)).reshape(-1))
                
                # ortho_project = x0t - At(A(x0t))
                ortho_project = x0t.reshape(1, -1) - At(A(x0t))
                parallel_project = At(measurement)
                projected = parallel_project + ortho_project

                recon_z = self.encode(projected.reshape(1, 3, 512, 512))
                z0_residue = torch.linalg.norm((recon_z - z0t).reshape(1, -1))
                
                omega = 1.0
                gamma = 0.5

                residue = omega * residue + gamma * z0_residue
                grad = torch.autograd.grad(residue, zt)[0]
                zt = zt_prime - grad

            pbar.set_postfix({'residue':residue.item()})

        # for the last step, do not add noise
        img = self.decode(z0t)
        img = (img / 2 + 0.5).clamp(0, 1)
        return img.detach().cpu()

@register_solver(name='p2l')
class P2LSolver(TRegSolver):
    def __init__(self,
                 solver_config: Dict,
                 model_key:str="botp/stable-diffusion-v1-5",
                 device: Optional[torch.device]=None,
                 **kwargs):
        super().__init__(solver_config, model_key, device, **kwargs)


    def loss_c(self, A, y, zt, c, at, t):
        noise_pred = self.predict_noise(zt, t, c, c, 1.0)
        z0t = (zt - (1-at).sqrt() * noise_pred)/at.sqrt()
        x0t = self.decode(z0t)
        dc_loss = torch.nn.functional.mse_loss(y, A(x0t)).reshape(-1)
        return dc_loss 

    @torch.autocast(device_type='cuda', dtype=torch.float16)
    def sample(self,
               measurement,
               operator,
               cfg_guidance=1.0,
               prompt=["",""],
               **kwargs):

        A = operator.A
        eta = 0.0  

        uc, c = self.get_text_embed(null_prompt=prompt[0], prompt=prompt[1])
        orig_c = c.clone()
        c = c.requires_grad_(True)
        optim_c = torch.optim.Adam([c], lr=1e-4)

        # initialize z_T
        zt = torch.randn((1,4,64,64)).to(self.device)
        zt.requires_grad = True
        
        self.unet = self.unet.requires_grad_(False)

        # loop
        pbar = tqdm(self.scheduler.timesteps, desc='P2L')
        for step, t in enumerate(pbar):
            prev_t = t - self.skip
            at = self.scheduler.alphas_cumprod[t]
            at_prev = self.scheduler.alphas_cumprod[prev_t] if prev_t >= 0 else self.final_alpha_cumprod
            
            with torch.enable_grad():
                # prompt tuning
                loss_c = self.loss_c(A, measurement, zt.detach(), c, at, t)
                optim_c.zero_grad()
                loss_c.backward()
                optim_c.step()
                
                c = c.detach()
                
                zt.requires_grad = True
                # sampling
                noise_pred = self.predict_noise(zt, t, uc, c, cfg_guidance)
                z0t = (zt - (1-at).sqrt() * noise_pred) / at.sqrt()

                # Equation (12) of DDIM
                noise = torch.randn_like(z0t).to(self.device)
                sig = eta * ((1-at_prev)/(1-at)).sqrt() * (1-at/at_prev).sqrt()
                zt_prime = at_prev.sqrt() * z0t + (1-at_prev-sig**2).sqrt() * noise_pred + sig * noise

                # data consistency
                x0t = self.decode(z0t)

                residue = torch.linalg.norm((measurement - A(x0t)).reshape(-1))
                grad = torch.autograd.grad(residue, zt, retain_graph=True)[0]
                zt = zt_prime - grad
                zt = zt.detach()

            pbar.set_postfix({'residue':residue.item()})

        # for the last step, do not add noise
        img = self.decode(z0t)
        img = (img / 2 + 0.5).clamp(0, 1)
        return img.detach().cpu()
