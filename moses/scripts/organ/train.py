import torch
import rdkit
import random
import numpy as np

from moses.organ import ORGAN, ORGANTrainer, get_parser as organ_parser
from moses.script_utils import add_train_args, read_smiles_csv, set_seed
from moses.utils import CharVocab

from moses.metrics import mapper, get_mol, fraction_valid, morgan_similarity, remove_invalid, \
                                    fragment_similarity, scaffold_similarity, fraction_passes_filters, \
                                    fraction_unique, internal_diversity

from multiprocessing import Pool

lg = rdkit.RDLogger.logger()
lg.setLevel(rdkit.RDLogger.CRITICAL)


def get_parser():
    parser = add_train_args(organ_parser())
    return add_args_for_reward_func(parser)


def add_args_for_reward_func(parser):
    parser.add_argument('--n_ref', type=int, default=500,
                        help='Number of reference molecules (sampling from training data)')
    parser.add_argument('--n_jobs', type=int, default=1,
                        help='Number of processes to run metrics')

    return parser


def parse_device_id(device):
    if device == 'cpu':
        return -1

    device = device.split(':')
    if len(device) > 1:
        return int(device[1])

    return 0


def nan2zero(value):
    if value == np.nan:
        return 0

    return value


def buid_reward_fn(config, train, pool, gpu):
    def reward(gen):
        rewards = []
        n_metrics = 7

        ref = random.sample(train, config.n_ref)
        ref = remove_invalid(ref, canonize=True, n_jobs=pool)
        ref_mols = mapper(pool)(get_mol, ref)

        nb = len(gen) // config.rollouts
        for i in range(0, nb):  # hack for metrics
            rollout_gen = gen[i::nb]
            current_reward = fraction_valid(rollout_gen, n_jobs=pool)

            if current_reward > 0:
                rollout_gen = remove_invalid(rollout_gen, canonize=True, n_jobs=pool)
                rollout_gen_mols = mapper(pool)(get_mol, rollout_gen)

                if len(rollout_gen) > 1:
                    current_reward += fraction_unique(rollout_gen, n_jobs=pool)
                    current_reward += scaffold_similarity(ref_mols, rollout_gen_mols, n_jobs=pool)
                    current_reward += fragment_similarity(ref_mols, rollout_gen_mols, n_jobs=pool)
                    current_reward += nan2zero(internal_diversity(rollout_gen_mols, n_jobs=pool))
                    current_reward += nan2zero(morgan_similarity(ref_mols, rollout_gen_mols, n_jobs=pool, gpu=gpu))

                current_reward += fraction_passes_filters(rollout_gen_mols, n_jobs=pool)

            rewards.extend([current_reward / n_metrics] * config.rollouts)

        return rewards

    return reward


def main(config):
    set_seed(config.seed)

    train = read_smiles_csv(config.train_load)
    vocab = CharVocab.from_data(train)
    device = torch.device(config.device)

    config = torch.load('pconfig.pt')
    vocab = torch.load('pvocab.pt')
    model_state = torch.load('pmodel.pt')

    config.generator_pretrain_epochs = 0
    config.discriminator_pretrain_epochs = 0
    config.pg_iters = 1000
    config.discriminator_updates = 500

    with Pool(config.n_jobs) as pool:
        reward_func = buid_reward_fn(config, train, pool, parse_device_id(config.device))

        model = ORGAN(vocab, config, reward_func)
        model.load_state_dict(model_state)
        model = model.to(device)

        trainer = ORGANTrainer(config)
        trainer.fit(model, train)

    torch.save(model.state_dict(), config.model_save)
    torch.save(config, config.config_save)
    torch.save(vocab, config.vocab_save)


if __name__ == '__main__':
    parser = get_parser()
    config = parser.parse_known_args()[0]
    main(config)
