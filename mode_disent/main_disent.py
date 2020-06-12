import os
import argparse
import torch
import gym
from pprint import pprint
from datetime import datetime
from easydict import EasyDict as edict
import json

from code_slac.env.ordinary_env import OrdinaryEnvForPytorch
from mode_disent.agent import DisentAgent
from mode_disent.utils.utils import parse_args


def run():
    # Parse arguments from command line
    args = parse_args()

    dir_name = args.env_info.env_id
    base_dir = os.path.join('logs', dir_name)
    if args.log_folder is not None:
        args.log_dir = os.path.join(base_dir, args.log_folder)
    else:
        args.log_dir = os.path.join(base_dir, args.run_id)
    args.run_hp = args.copy()

    args.device = args.device if torch.cuda.is_available() else "cpu"
    if args.env_info.env_type == 'normal':
        args.env = OrdinaryEnvForPytorch(args.env_info.env_id)
    elif args.env_info.env_type == 'dm_control':
        args.env = DmControlEnvForPytorch(
            domain_name=args.env_info.domain_name,
            task_name=args.env_info.task_name,
            action_repeat=args.env_info.action_repeat,
            obs_type=args.env_info.obs_type
        )

    args.skill_policy = torch.load(args.skill_policy_path)['evaluation/policy']
    args.dyn_latent = torch.load(args.dynamics_model_path)\
        if args.dynamics_model_path is not None else None
    args.mode_latent = torch.load(args.mode_model_path)\
        if args.mode_model_path is not None else None

    args.run_id = f'mode_disent{args.seed}-{datetime.now().strftime("%Y%m%d-%H%M")}'
    if args.run_comment is not None:
        args.run_id += str(args.run_comment)

    args.pop('run_comment')
    args.pop('skill_policy_path')
    args.pop('log_folder')
    args.pop('dynamics_model_path')
    args.pop('mode_model_path')
    args.pop('env_info')

    agent = DisentAgent(**args).run()


# python main_disent.py --config ./config/config.json
if __name__ == "__main__":
    run()
