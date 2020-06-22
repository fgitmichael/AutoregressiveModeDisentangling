import os
import json
import numpy as np
import torch
import torch.nn.functional as F
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
import matplotlib
import matplotlib.pyplot as plt
import warnings
matplotlib.use('Agg')

from mode_disent_no_ssm.utils.skill_policy_wrapper import DiaynSkillPolicyWrapper
from mode_disent.memory.memory import MyLazyMemory
from mode_disent.env_wrappers.rlkit_wrapper import NormalizedBoxEnvForPytorch
from mode_disent.utils.mmd import compute_mmd_tutorial
from mode_disent_no_ssm.network.mode_model import ModeLatentNetwork
from code_slac.utils import calc_kl_divergence, update_params
from code_slac.network.base import create_linear_network


class DisentTrainerNoSSM:

    def __init__(self,
                 env: NormalizedBoxEnvForPytorch,
                 log_dir,
                 min_steps_sampling,
                 batch_size,
                 num_sequences,
                 train_steps,
                 lr,
                 mode_dim,
                 num_mode_repitions,
                 rnn_dim,
                 num_rnn_layers,
                 rnn_dropout,
                 hidden_units_mode_encoder,
                 std_decoder,
                 mode_latent_model: ModeLatentNetwork,
                 hidden_units_obs_encoder,
                 hidden_units_action_decoder,
                 memory_size,
                 skill_policy: DiaynSkillPolicyWrapper,
                 log_interval,
                 info_loss_params,
                 run_id,
                 run_hp,
                 device,
                 leaky_slope=0.2,
                 seed=0,
                 ):

        self.env = env
        self.observation_shape = self.env.observation_space.shape
        self.action_shape = self.env.action_space.shape

        self.feature_dim = int(self.observation_shape[0])
        self.mode_dim = mode_dim
        self.num_sequences = num_sequences
        self.min_steps_sampling = min_steps_sampling
        self.batch_size = batch_size
        self.train_steps = train_steps
        self.run_id = run_id
        self.learn_steps = 0
        self.episodes = 0

        self.seed = None
        self._set_seed(seed)

        self.device = None
        self._set_device(device)

        self.obs_encoder = create_linear_network(
            input_dim=self.observation_shape[0],
            output_dim=self.feature_dim,
            hidden_units=hidden_units_obs_encoder
        ).to(self.device)

        if mode_latent_model is None:
            self.mode_latent_model = ModeLatentNetwork(
                mode_dim=self.mode_dim,
                representation_dim=self.feature_dim,
                rnn_dim=rnn_dim,
                num_rnn_layers=num_rnn_layers,
                rnn_dropout=rnn_dropout,
                hidden_units_mode_encoder=hidden_units_mode_encoder,
                hidden_units_action_decoder=hidden_units_action_decoder,
                num_mode_repeat=num_mode_repitions,
                feature_dim=self.feature_dim,
                action_dim=self.action_shape[0],
                std_decoder=std_decoder,
                device=self.device,
                leaky_slope=leaky_slope,
            ).to(self.device)
            self.mode_model_loaded = False

        else:
            self.mode_latent_model = mode_latent_model.to(self.device)
            self.mode_model_loaded = True

        self.optim = Adam(self.mode_latent_model.parameters(), lr=lr)

        self.memory = MyLazyMemory(
            state_rep=True,
            capacity=memory_size,
            num_sequences=self.num_sequences,
            observation_shape=self.observation_shape,
            action_shape=self.action_shape,
            device=self.device
        )

        self.log_dir = log_dir
        self.model_dir = os.path.join(self.log_dir, 'model', str(self.run_id))
        self.summary_dir = os.path.join(self.log_dir, 'summary', str(self.run_id))
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        if not os.path.exists(self.summary_dir):
            os.makedirs(self.summary_dir)

        hparam_save_path = os.path.join(self.model_dir, 'run_hyperparameter.json')
        with open(hparam_save_path, 'w', encoding='utf-8') as f:
            json.dump(run_hp, f, ensure_ascii=False, indent=4)

        self.writer = SummaryWriter(log_dir=self.summary_dir)
        self.log_interval = log_interval

        self.skill_policy = skill_policy
        self.info_loss_params = info_loss_params
        self.num_skills = self.skill_policy.num_skills
        self.steps = np.zeros(shape=self.num_skills, dtype=np.int)

    def run_training(self):
        self._sample_sequences(
            memory_to_fill=self.memory,
            min_steps=self.min_steps_sampling,
            step_cnt=self.steps
        )

        self._train()

        self._save_models()

    def _train(self):
        for _ in tqdm(range(self.train_steps)):
            self._learn_step()

    def _learn_step(self):
        sequences = self.memory.sample_sequence(self.batch_size)

        loss = self._calc_loss(sequences)
        update_params(self.optim, self.mode_latent_model, loss)

        self.learn_steps += 1

    def _calc_loss(self, sequence):
        actions_seq = sequence['actions_seq']
        features_seq = self.obs_encoder(sequence['states_seq'])
        skill_seq = sequence['skill_seq']
        skill_seq_np_squeezed =\
            self._tensor_to_numpy(skill_seq.float().mean(dim=1)) \
            .astype(np.uint8).squeeze()

        # Posterior and prior
        mode_post = self.mode_latent_model.sample_mode_posterior(features_seq=features_seq)
        mode_pri = self.mode_latent_model.sample_mode_prior(self.batch_size)

        # KLD
        kld = calc_kl_divergence([mode_post['dists']],
                                 [mode_pri['dists']])

        # MMD
        mmd = compute_mmd_tutorial(mode_pri['samples'],
                                   mode_post['samples'])

        # Reconstruction
        actions_seq_recon = self.mode_latent_model.action_decoder(
            state_rep_seq=features_seq[:, :-1, :],
            mode_sample=mode_post['samples']
        )

        # Reconstruction loss
        ll = actions_seq_recon['dists'].log_prob(actions_seq).mean(dim=0).sum()
        mse = F.mse_loss(actions_seq_recon['samples'], actions_seq)

        # Classic beta-VAE loss
        beta = 1.
        classic_loss = beta * kld - ll

        # Info-VAE loss
        alpha = self.info_loss_params.alpha
        lamda = self.info_loss_params.lamda
        kld_info = (1 - alpha) * kld
        mmd_info = (alpha + lamda - 1) * mmd
        info_loss = mse + kld_info + mmd_info
        if self.info_loss_params.kld_diff_desired is not None:
            kld_desired_scalar = self.info_loss_params.kld_diff_desired
            kld_desired = torch.tensor(kld_desired_scalar).to(self.device)
            kld_diff_control = 0.07 * F.mse_loss(kld_desired, kld)
            info_loss += kld_diff_control

        # Logging
        base_str_stats = 'Mode Model stats/'
        base_str_info = 'Mode Model info-vae/'
        base_str_mode_map = 'Mode Model'
        if self._is_interval(self.log_interval, self.learn_steps):
            self._summary_log_mode(base_str_stats + 'log-liklyhood', ll)
            self._summary_log_mode(base_str_stats + 'mse', mse)
            self._summary_log_mode(base_str_stats + 'kld', kld)
            self._summary_log_mode(base_str_stats + 'mmd', mmd)

            self._summary_log_mode(base_str_info + 'kld info-weighted', kld_info)
            self._summary_log_mode(base_str_info + 'mmd info weighted', mmd_info)
            self._summary_log_mode(
                base_str_info + 'loss on latent', mmd_info+ kld_info)

            mode_map_fig = self._plot_mode_map(
                skill_seq=skill_seq,
                mode_post_samples=mode_post['samples']
            )
            self._save_fig(
                locations=['writer'],
                fig=mode_map_fig,
                base_str=base_str_mode_map
            )

        return info_loss

    def _sample_sequences(self,
                          memory_to_fill: MyLazyMemory,
                          min_steps,
                          step_cnt: np.ndarray):
        skill = 0
        while np.sum(step_cnt) < min_steps:
            self._sample_equal_skill_dist(memory=memory_to_fill,
                                          skill=skill,
                                          step_cnt=step_cnt)

            skill = min(skill + 1, (skill + 1) % self.num_skills)
            self.episodes += 1

        print(self.steps)
        memory_to_fill.skill_histogram(writer=self.writer)

    def _sample_equal_skill_dist(self,
                                 memory: MyLazyMemory,
                                 skill,
                                 step_cnt: np.ndarray):
        episode_steps = 0
        self.skill_policy.set_skill(skill)

        obs = self.env.reset()
        memory.set_initial_state(obs)

        next_obs = obs
        done = False
        while self.steps[skill] <= np.max(self.steps):
            if done:
                next_obs = self.env.reset()

            action = self.skill_policy.get_action(
                obs_denormalized=self.env.denormalize(next_obs)
                if self.env.state_normalization else next_obs
            )
            next_state, reward, done, _ = self.env.step(action)

            episode_steps += 1
            step_cnt[skill] += 1

            seq_pushed = memory.append(action=action,
                                       skill=np.array([skill], dtype=np.uint8),
                                       state=next_state,
                                       done=np.array([done], dtype=np.bool))
            if seq_pushed:
                break

        print(f'episode: {self.episodes:<4}  '
              f'episode_steps: {episode_steps:<4}  '
              f'skill: {skill: <4}  ')

    def _plot_mode_map(self,
                       skill_seq,
                       mode_post_samples,
                       ):
        """
        Args:
            skill_seq           : (N, S, 1) - tensor
            mode_post_samples   : (N, 2) - tensor
            base_str            : string
        """
        plot_dim = 2
        if not self.mode_dim == plot_dim:
            warnings.warn(f'Warning: Mode-Dimension is not equal to {plot_dim:<2}.'
                          f'No mode map is plotted')
            return None

        colors = ['b', 'g', 'r', 'c', 'm', 'y', 'k',
                  'darkorange', 'gray', 'lightgreen']

        if self.num_skills > len(colors):
            raise ValueError(f'Not more than then {len(colors):<3} '
                             f'skill supported for mode'
                             f'plotting right now (more color needed)')

        assert mode_post_samples.shape == torch.Size((self.batch_size, plot_dim))

        skill_seq = self._tensor_to_numpy(skill_seq.float().mean(dim=1))\
            .astype(np.uint8).squeeze()
        mode_post_samples = self._tensor_to_numpy(mode_post_samples)

        plt.interactive(False)
        _, axes = plt.subplots()
        lim = [-3., 3.]
        axes.set_ylim(lim)
        axes.set_xlim(lim)

        for skill in range(skill_seq.max() + 1):
            bool_idx = skill_seq == skill
            plt.scatter(mode_post_samples[bool_idx, 0],
                        mode_post_samples[bool_idx, 1],
                        label=skill,
                        c=colors[skill])

        axes.legend()
        axes.grid(True)
        fig = plt.gcf()

        return fig

    def _set_seed(self, seed):
        self.seed = seed
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        self.env.seed(self.seed)

    def _set_device(self, device_str):
        self.device = torch.device(
            device_str if torch.cuda.is_available() else "cpu"
        )
        print("device set to " + str(self.device))

    def _save_models(self):
        path_name = os.path.join(self.model_dir, 'mode_model.pkl')
        torch.save(self.mode_latent_model, path_name)

    def _summary_log_mode(self, data_name, data):
        if type(data) == torch.Tensor:
            data = data.detach().cpu().item()
        self.writer.add_scalar(data_name, data, self.learn_steps)

    @staticmethod
    def _is_interval(log_interval, steps):
        return True if steps % log_interval == 0 else False

    @staticmethod
    def _tensor_to_numpy(tensor: torch.tensor):
        return tensor.detach().cpu().numpy()

    def _numpy_to_tensor(self, nd_array: np.ndarray):
        return torch.from_numpy(nd_array).to(self.device)

    def _save_fig(self, fig, locations: list, base_str):
        for loc in locations:
            if loc == 'writer':
                self.writer.add_figure(base_str + 'mode mapping',
                                       fig,
                                       global_step=self.learn_steps)

            elif loc == 'file':
                path_name_fig = os.path.join(self.model_dir, 'mode_mapping.fig')
                torch.save(obj=fig, f=path_name_fig)

            else:
                raise NotImplementedError(f'Location {loc} is not implemented')

        plt.clf()








