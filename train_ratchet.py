import argparse
import json
import os
import pickle
from argparse import Namespace
from collections import Counter
from copy import deepcopy

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from scipy import stats
from tqdm import tqdm

from misc.sampler import CartesianSampler
from misc.utils import logging_rneep, save_checkpoint
from model.net import EmbeddingNEEP


def train(opt, model, optim, trajs, sampler):
    model.train()
    batch, next_batch = next(sampler)

    s_prev = trajs[batch].to(opt.device)
    s_next = trajs[next_batch].to(opt.device)
    ent_production = model(s_prev, s_next)
    optim.zero_grad()

    # The objective function J. Equation (2)
    loss = (-ent_production + torch.exp(-ent_production)).mean()
    loss.backward()
    optim.step()
    return loss.item()


def validate(opt, model, trajs, sampler):
    model.eval()
    sampler.eval()

    ret = []
    loss = 0
    with torch.no_grad():
        for batch, next_batch in sampler:
            s_prev = trajs[batch].to(opt.device)
            s_next = trajs[next_batch].to(opt.device)

            ent_production = model(s_prev, s_next)
            entropy = ent_production.cpu().squeeze().numpy()
            ret.append(entropy)
            loss += (-ent_production + torch.exp(-ent_production)).sum().cpu().item()
    loss = loss / sampler.size
    ret = np.concatenate(ret)
    ret = ret.reshape(trajs.shape[0], -1)
    return ret, loss


def main(opt):

    print("=" * 80)
    print("Generating discrete flashing ratchet trajectories")
    data = simulation(2, opt.n_step, opt.potential, seed=0)
    trainset = data[0]
    testset = data[1]
    print("Done")
    print("=" * 80)

    use_cuda = not opt.no_cuda and torch.cuda.is_available()
    torch.manual_seed(opt.seed)
    opt.device = torch.device("cuda" if use_cuda else "cpu")
    opt.seq_len = 2

    model = EmbeddingNEEP(opt)
    model = model.to(opt.device)
    optim = torch.optim.Adam(model.parameters(), opt.lr, weight_decay=opt.wd)

    trajs_t = torch.from_numpy(trainset).to(opt.device).long().view(1, -1)
    test_trajs_t = torch.from_numpy(testset).to(opt.device).long().view(1, -1)

    train_sampler = CartesianSampler(1, opt.n_step, opt.batch_size)
    test_sampler = CartesianSampler(1, opt.n_step, opt.test_batch_size, train=False)

    ret_train = []
    ret_test = []

    if not os.path.exists(opt.save):
        os.makedirs(opt.save)

    for i in tqdm(range(1, opt.n_iter + 1)):
        if i % opt.record_freq == 0 or i == 1:
            preds, train_loss = validate(opt, model, trajs_t, train_sampler)
            train_log = logging_rneep(i, train_loss, opt.seq_len, preds)

            preds, test_loss = validate(opt, model, test_trajs_t, test_sampler)
            test_log = logging_rneep(i, test_loss, opt.seq_len, preds, train=False)
            if i == 1:
                best_loss = test_loss
                best_pred_rate = test_log["pred_rate"]
            else:
                is_best = test_loss < best_loss
                if is_best:
                    best_loss = test_loss
                    best_pred_rate = test_log["pred_rate"]
                save_checkpoint(
                    {
                        "iteration": i,
                        "state_dict": model.state_dict(),
                        "best_loss": best_loss,
                        "best_pred_rate": best_pred_rate,
                        "optimizer": optim.state_dict(),
                    },
                    is_best,
                    opt.save,
                )
            test_log["best_loss"] = best_loss
            test_log["best_pred_rate"] = best_pred_rate
            ret_train.append(train_log)
            ret_test.append(test_log)
            train_sampler.train()

        train(opt, model, optim, trajs_t, train_sampler)


    train_df = pd.DataFrame(ret_train)
    test_df = pd.DataFrame(ret_test)

    train_df.to_csv(os.path.join(opt.save, "train_log.csv"), index=False)
    test_df.to_csv(os.path.join(opt.save, "test_log.csv"), index=False)
    opt.device = "cuda" if use_cuda else "cpu"
    hparams = json.dumps(vars(opt))
    with open(os.path.join(opt.save, "hparams.json"), "w") as f:
        f.write(hparams)


if __name__ == "__main__":
    # Training settings
    parser = argparse.ArgumentParser(
        description="Neural Entropy Production Estimator for full information DFR"
    )
    parser.add_argument(
        "--potential", type=float, default=2.0, metavar="V", help="Potential"
    )
    parser.add_argument(
        "--n-step",
        "-L",
        type=int,
        default=1000000,
        metavar="L",
        help="number of step for each trajectory (default: 1000000)",
    )
    parser.add_argument(
        "--save",
        default="",
        type=str,
        metavar="PATH",
        help="path to save result (default: none)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4096,
        metavar="N",
        help="input batch size for training (default: 4096)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=10000,
        metavar="N",
        help="input batch size for testing (default: 10000)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.0001,
        metavar="LR",
        help="learning rate (default: 0.0001)",
    )
    parser.add_argument(
        "--wd",
        type=float,
        default=5e-5,
        metavar="LR",
        help="learning rate (default: 5e-5)",
    )
    parser.add_argument(
        "--n-token",
        type=int,
        default=6,
        metavar="N",
        help="number of token (default: 6)",
    )
    parser.add_argument(
        "--n-iter",
        type=int,
        default=50000,
        metavar="N",
        help="number of iteration to train (default: 50000)",
    )
    parser.add_argument(
        "--n-hidden",
        type=int,
        default=128,
        metavar="N",
        help="number of hidden neuron (default: 128)",
    )
    parser.add_argument(
        "--n-layer",
        type=int,
        default=1,
        metavar="N",
        help="number of layer (default: 1)",
    )
    parser.add_argument(
        "--record-freq",
        type=int,
        default=100,
        metavar="N",
        help="number of iteration to train (default: 100)",
    )
    parser.add_argument(
        "--no-cuda", action="store_true", default=False, help="disables CUDA training"
    )
    parser.add_argument(
        "--seed", type=int, default=1, metavar="S", help="random seed (default: 1)"
    )

    opt = parser.parse_args()
    main(opt)
