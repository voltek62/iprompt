"""Note: this scripts hacks 03_train_prefix.py with different default parameters / data to run for fMRI.
After finding categories, use other fMRI script to output the appropriate logits.
"""

from typing import Dict, List, Tuple

import datasets
import functools
import os
import random
import string
import numpy as np
import time
import torch
from torch import nn
import transformers
import matplotlib.pyplot as plt
import argparse
from transformers import pipeline
from transformers import AutoTokenizer, AutoModel, AutoModelForCausalLM
from copy import deepcopy
import pandas as pd
from tqdm import tqdm
from collections import defaultdict
from iprompt.prefix import (
    AutoPrompt, iPrompt,
    PrefixLoss, PrefixModel,
    PromptTunedModel, HotFlip, GumbelPrefixModel
)
from iprompt.data_utils import neuro
import pandas as pd
from datasets import Dataset
import iprompt.data as data
import logging
import pickle as pkl
from torch.utils.data import DataLoader
from datetime import datetime


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


model_cls_dict = {
    'autoprompt': AutoPrompt,
    'genetic': iPrompt,  # outdated alias
    'iprompt': iPrompt,
    'gumbel': GumbelPrefixModel,
    'hotflip': HotFlip,
    'prompt_tune': PromptTunedModel,
}


def train_model(
    args: argparse.Namespace,
    r: Dict[str, List],
    dset: datasets.Dataset,
    model: PrefixModel,
    tokenizer: transformers.PreTrainedTokenizer
):
    """
    Trains a model, either by optimizing continuous embeddings or finding an optimal discrete embedding.

    Params
    ------
    r: dict
        dictionary of things to save
    """

    r['train_start_time'] = time.time()
    model.train()

    model = model.to(device)
    dataloader = DataLoader(
        dset, batch_size=args.batch_size, shuffle=True, drop_last=False)

    # optimizer
    optim = torch.optim.AdamW(model.trainable_params, lr=args.lr)

    assert model.training

    # Compute loss only over possible answers to make task easier
    possible_answer_ids = []
    for batch in dataloader:
        y_text = [answer for answer in batch['output']]
        y_tokenized = tokenizer(y_text, return_tensors='pt', padding='longest')
        # only test on the single next token
        true_next_token_ids = y_tokenized['input_ids'][:, 0]
        possible_answer_ids.extend(true_next_token_ids.tolist())

    possible_answer_ids = torch.tensor(possible_answer_ids)
    num_unique_answers = len(set(possible_answer_ids.tolist()))
    assert num_unique_answers > 0, "need multiple answers for multiple choice"
    random_acc = 1 / num_unique_answers * 100.0
    majority_count = (
        possible_answer_ids[:, None] == possible_answer_ids[None, :]).sum(dim=1).max()
    majority_acc = majority_count * 100.0 / len(possible_answer_ids)
    print(
        f"Training with {num_unique_answers} possible answers / random acc {random_acc:.1f}% / majority acc {majority_acc:.1f}%")

    vocab_size = len(tokenizer.vocab)

    if args.mask_possible_answers:
        possible_answer_mask = (
            torch.arange(start=0, end=vocab_size)[:, None]
            ==
            possible_answer_ids[None, :]
        ).any(dim=1).to(device)
    else:
        possible_answer_mask = None

    stopping_early = False
    total_n_steps = 0
    total_n_datapoints = 0
    for epoch in range(args.n_epochs):
        model.pre_epoch()

        all_losses = []

        total_n = 0
        total_n_correct = 0
        pbar = tqdm(enumerate(dataloader), total=len(dataloader))
        for idx, batch in pbar:
            total_n_steps += 1
            if (args.n_shots > 1) and (args.single_shot_loss):
                batch['input'] = batch['last_input']
            x_text, y_text = model.prepare_batch(batch=batch)

            tok = functools.partial(
                model.tokenizer, return_tensors='pt', padding='longest',
                truncation=True, max_length=args.max_length)
            x_tokenized = tok(x_text).to(device)
            y_tokenized = tok(y_text).to(device)
            full_text_tokenized = tok(batch['text']).to(device)

            loss, n_correct = model.compute_loss_and_call_backward(
                x_tokenized=x_tokenized,
                y_tokenized=y_tokenized,
                possible_answer_mask=possible_answer_mask,
                full_text_tokenized=full_text_tokenized,
            )

            r["all_losses"].append(loss)
            r["all_n_correct"].append(n_correct)

            total_n += len(x_text)
            total_n_datapoints += len(x_text)
            total_n_correct += n_correct

            all_losses.append(loss)
            pbar.set_description(f"Loss = {loss:.3f}")

            if not args.accum_grad_over_epoch:
                # if hotflip, autoprompt, etc., grad will be zero
                optim.step()
                optim.zero_grad()

            # Early stopping, check after step
            model_check_early_stop = model.check_early_stop()
            if model_check_early_stop:
                print("model_check_early_stop returned true")
            if (total_n_datapoints > args.max_n_datapoints) or (total_n_steps > args.max_n_steps) or model_check_early_stop:
                stopping_early = True
                break

        if stopping_early:
            print(f"Ending epoch {epoch} early...")
        avg_loss = sum(all_losses) / len(all_losses)
        print(f"Epoch {epoch}. average loss = {avg_loss:.3f} / {total_n_correct} / {total_n} correct ({total_n_correct/total_n*100:.2f}%)")

        # save stuff
        for key, val in model.compute_metrics().items():
            r[key].append(val)

        # r['losses'].append(avg_loss)
        if epoch % args.epoch_save_interval == 0:
            os.makedirs(save_dir, exist_ok=True)
            pkl.dump(r, open(os.path.join(save_dir, 'results.pkl'), 'wb'))

        model.post_epoch(dataloader=dataloader,
                         possible_answer_mask=possible_answer_mask)

        if args.accum_grad_over_epoch:
            optim.step()
            optim.zero_grad()

        # Early stopping, check after epoch
        if stopping_early:
            print(
                f"Stopping early after {total_n_steps} steps and {total_n_datapoints} datapoints")
            break

    # Serialize model-specific stuff (prefixes & losses for autoprompt, embeddings for prompt tuning, etc.)
    r.update(model.serialize())

    # save whether prefixes fit the template
    if "prefixes" in r:
        r["prefixes__check_answer_func"] = list(
            map(check_answer_func, r["prefixes"]))

    r['train_end_time'] = time.time()
    r['train_time_elapsed'] = r['train_end_time'] - r['train_start_time']

    pkl.dump(r, open(os.path.join(save_dir, 'results.pkl'), 'wb'))

    return r


def eval_model_with_set_prefix(
    args: argparse.Namespace,
    r: Dict[str, List],
    dataloader: DataLoader,
    model: PrefixModel,
    tokenizer: transformers.PreTrainedTokenizer
) -> Tuple[float, float]:
    """
    Evaluates a model based on set prefix. May be called multiple times with different prefixes

    Params
    ------
    r: dict
        dictionary of things to save

    Returns: Tuple[float, float]
        average loss, accuracy per sample over eval dataset
    """
    pbar = tqdm(enumerate(dataloader), total=len(dataloader),
                desc='evaluating data', colour='red', leave=False)
    total_loss = 0.0
    total_n = 0
    total_n_correct = 0
    for idx, batch in pbar:
        x_text, y_text = model.prepare_batch(batch=batch)

        tok = functools.partial(
            model.tokenizer, return_tensors='pt', padding='longest')
        x_tokenized = tok(x_text).to(device)
        y_tokenized = tok(y_text).to(device)
        full_text_tokenized = tok(batch['text']).to(device)

        with torch.no_grad():
            _input_ids, loss, n_correct = model._compute_loss_with_set_prefix(
                original_input_ids=x_tokenized.input_ids,
                next_token_ids=y_tokenized.input_ids,
                possible_answer_mask=None,  # TODO: implement eval verbalizer
                prefix_ids=None,
            )

        total_loss += loss.item()
        total_n += len(x_text)
        total_n_correct += n_correct

        pbar.set_description(
            f"Acc = {total_n_correct}/{total_n} {(total_n_correct/total_n*100):.2f}%")

    return (total_loss / total_n), (total_n_correct / total_n)


def eval_model(
    args: argparse.Namespace,
    r: Dict[str, List],
    dset: datasets.Dataset,
    model: PrefixModel,
    tokenizer: transformers.PreTrainedTokenizer
):
    """
    Evaluates a model based on the learned prefix(es).

    Params
    ------
    r: dict
        dictionary of things to save
    """
    r["test_start_time"] = time.time()
    model.eval()
    dataloader = DataLoader(
        dset, batch_size=args.batch_size, shuffle=False, drop_last=False)

    if r["prefixes"]:
        # if we specified multiple prefixes (autoprompt or genetic), let's evaluate them all!
        for prefix_ids in tqdm(r["prefix_ids"], desc="evaluating prefixes"):
            model._set_prefix_ids(new_ids=torch.tensor(prefix_ids).to(device))

            loss, acc = eval_model_with_set_prefix(
                args=args, r=r, dataloader=dataloader, model=model, tokenizer=tokenizer
            )

            r["prefix_test_loss"].append(loss)
            r["prefix_test_acc"].append(acc)
        r["num_prefixes_used_for_test"] = len(r["prefixes"])

    else:
        # otherwise, there's just one prefix (like for prompt tuning) so just run single eval loop.
        loss, acc = eval_model_with_set_prefix(
            args=args, r=r, dataloader=dataloader, model=model, tokenizer=tokenizer
        )
        r["prefix_test_acc"] = loss
        r["prefix_test_loss"] = acc
        r["num_prefixes_used_for_test"] = 1

    r["test_end_time"] = time.time()
    r["test_time_elapsed"] = r["test_end_time"] - r["test_start_time"]
    pkl.dump(r, open(os.path.join(save_dir, 'results.pkl'), 'wb'))
    return r


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--batch_size', type=int, default=500,
                        help='batch size for training')
    parser.add_argument('--seed', type=int, default=1,
                        help='random seed')
    parser.add_argument('--max_n_datapoints', type=int, default=10,
                        help='max number of datapoints for training')
    parser.add_argument('--early_stopping_steps', type=int, default=-1,
                        help='if > 0, number of steps until stopping early after no improvement')
    parser.add_argument('--max_digit', type=int, default=100,
                        help='maximum value of each digit in summand')
    parser.add_argument('--template_num_init_string', type=int, default=0,
                        help='the number of the manually-specified prefix to be initialize with')
    parser.add_argument('--template_num_task_phrasing', type=int, default=0,
                        help='the number of the manual template for any given task (number of options varies with task')
    parser.add_argument('--save_dir', type=str, default='results',
                        help='directory for saving')
    parser.add_argument('--epoch_save_interval', type=int, default=1,
                        help='interval to save results')
    parser.add_argument('--iprompt_generation_repetition_penalty', type=float, default=2.0,
                        help='repetition penalty for iprompt generations')
    parser.add_argument('--lr', type=float, default=1e-4,
                        help='learning rate')
    parser.add_argument('--gamma', type=float, default=0.0,
                        help='hparam: weight for language modeling loss')
    parser.add_argument('--task_name', type=str, default='add_two',
                        choices=(data.TASKS.keys() - {'SUFFIX'}),
                        help='name of task')
    parser.add_argument('--task_name_list', nargs="*", default=None,
                        help='names of tasks as list; alternative to passing task_name')
    parser.add_argument('--n_shots', type=int, default=1,
                        help='number of shots in the prompt')
    parser.add_argument('--autoprompt_init_strategy', type=str, default='the',
                        choices=('random', 'the'), help='initialization strategy for discrete tokens')
    parser.add_argument('--max_length', type=int, default=128,
                        help='maximum length for inputs')
    parser.add_argument('--single_shot_loss', type=int, default=0,
                        help='if n_shots==0, load multiple shots but only use one compute loss')
    parser.add_argument('--mask_possible_answers', type=int, default=0,
                        help='only compute loss over possible answer tokens')
    parser.add_argument('--hotflip_num_candidates', type=int, default=10,
                        help='number of candidates to rerank, for hotflip')
    parser.add_argument('--accum_grad_over_epoch', type=int, default=0, choices=(0, 1),
                        help='should we clear gradients after a batch, or only at the end of the epoch?')

    parser.add_argument('--use_preprefix', type=int, default=1, choices=(0, 1),
                        help='whether to use a template pre-prefix')
    parser.add_argument('--iprompt_preprefix_str', type=str, default='',
                        help='Text like "Output the number that" or "Answer F/M if"...'
                        )
    parser.add_argument('--llm_float16', '--float16', '--parsimonious', type=int, default=0, choices=(0, 1),
                        help='if true, loads LLM in fp16 and at low-ram')

    # fMRI changed args
    parser.add_argument('--num_learned_tokens', type=int, default=1,
                        help='number of learned prefix tokens (for gumbel, hotflip, autoprompt, prompt-tuning)')
    parser.add_argument('--train_split_frac', type=float,
                        default=None, help='fraction for train-test split if desired')
    parser.add_argument('--n_epochs', type=int, default=50,
                        help='number of epochs for training')
    parser.add_argument('--max_dset_size', type=int,
                        default=10, help='maximum allowable dataset size')
    parser.add_argument('--max_n_steps', type=int, default=100,
                        help='max number of steps for training')
    parser.add_argument('--model_cls', type=str,
                        choices=model_cls_dict.keys(),
                        default='iprompt',
                        help='model type to use for training')
    parser.add_argument('--checkpoint', type=str, default="gpt2", # "EleutherAI/gpt-j-6B",
                        help='model checkpoint to use'
                        )
    parser.add_argument('--voxel_num', type=int, default=0, help='which voxel to model (swept from 0 to 15)'
                        )

    args = parser.parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    transformers.set_seed(args.seed)

    args.use_generic_query = 0

    if (args.mask_possible_answers) and (args.train_split_frac is not None):
        print("Warning: mask possible answers not supported for eval")

    # iterate over tasks
    if args.task_name_list is not None:
        logging.info('using task_name_list ' + str(args.task_name_list))
    else:
        args.task_name_list = [args.task_name]
    for task_idx, task_name in enumerate(args.task_name_list):
        print(f'*** Executing task {task_idx+1}/{len(args.task_name_list)}')
        # actually set the task
        args.task_name = task_name

        r = defaultdict(list)
        r.update(vars(args))
        logger = logging.getLogger()
        logging.basicConfig(level=logging.INFO)

        logger.info('loading model and data...')
        checkpoint = args.checkpoint
        tokenizer = AutoTokenizer.from_pretrained(checkpoint)
        tokenizer.pad_token = tokenizer.eos_token

        if args.llm_float16:
            lm = AutoModelForCausalLM.from_pretrained(
                checkpoint, output_hidden_states=False, pad_token_id=tokenizer.eos_token_id,
                revision="float16", torch_dtype=torch.float16, low_cpu_mem_usage=True
            )
        else:
            lm = AutoModelForCausalLM.from_pretrained(
                checkpoint, output_hidden_states=False, pad_token_id=tokenizer.eos_token_id
            )
        loss_func = PrefixLoss(gamma=args.gamma, tokenizer=tokenizer)

        # set up saving
        save_dir_unique = datetime.now().strftime("%b_%d_%H_%M_") + \
            ''.join(random.choices(string.ascii_lowercase, k=12))
        save_dir = os.path.join(args.save_dir, save_dir_unique)
        logging.info('saving to ' + save_dir)
        args.save_dir_unique = save_dir

        check_answer_func = lambda x: True    
        preprefix = 'The following list of words all belong to the same semantic category: '
        model = model_cls_dict[args.model_cls](
            args=args,
            loss_func=loss_func, model=lm, tokenizer=tokenizer, preprefix=preprefix
        )
        dset = neuro.fetch_permuted_word_list_for_voxel(vox_num=args.voxel_num, num_shuffles=15)

        logger.info('beginning training...')
        r = train_model(args=args, r=r, dset=dset,
                        model=model, tokenizer=tokenizer)
