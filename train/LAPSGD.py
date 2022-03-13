import math
import os
import random
import time
import torch
import torch.multiprocessing as mp
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torch.nn as nn
from collections import defaultdict
from dataloaders import get_dataloader
from models import get_model
from utilities.utils import bar
from utilities.utils import accuracy
from utilities.utils import test_epoch
from utilities.utils import result_save
from utilities.utils import LocalMetric
from utilities.utils import process_test_result
from utilities.utils import process_train_result
from utilities.utils import CosineAnnealingLR
from utilities.utils import MultiStepLR
from utilities.communicator import communicate_to_all
from utilities.results_summary import results_summary


def average(net, args, minibatch_counter):
    torch.cuda.set_device(args.devicerank)
    last_averaged_at = 0
    while True:
        with minibatch_counter.get_lock():
            minibatches = minibatch_counter.value
        if minibatches > 0:
            break
    dist.barrier()
    while True:
        with minibatch_counter.get_lock():
            minibatches = minibatch_counter.value
        maxepoch = torch.tensor([minibatches,
                                 -last_averaged_at]).float().cuda()
        dist.all_reduce(maxepoch, op=dist.ReduceOp.MAX)
        maxminibatches = maxepoch[0].item()
        if maxminibatches >= args.trainloaderlength * args.epochs:
            print("Reached MaxEpoch at rank ", args.commrank, maxepoch,
                  minibatches)
            break
        avg_freq = 1 if maxminibatches * 1.0 / args.trainloaderlength \
            < args.pre_post_epochs else args.averaging_freq

        if maxminibatches + maxepoch[1].item() >= avg_freq:
            # print("Averaging")
            communicate_to_all(list(net.parameters()), args, minibatches)
            last_averaged_at = minibatches


class opt(object):
    def __init__(self, parameters, lr, momentum, weight_decay, nesterov):
        self.parameters = parameters
        self.weight_decay = weight_decay
        self.momentum = momentum
        self.nesterov = nesterov
        self.lr = lr
        self.state = defaultdict(dict)

    def step(self, grad):
        for p, d_p in zip(self.parameters, grad):
            if d_p is None:
                continue
            if self.weight_decay != 0:
                d_p.add_(p.data, alpha=self.weight_decay)

            if self.momentum != 0:
                param_state = self.state[p]
                if 'momentum_buffer' not in param_state:
                    buf = param_state['momentum_buffer'] = torch.clone(
                        d_p).detach()
                else:
                    buf = param_state['momentum_buffer']
                    buf.mul_(self.momentum).add_(d_p)
                if self.nesterov:
                    d_p = d_p.add(buf, alpha=self.momentum)
                else:
                    d_p = buf

            p.data.add_(d_p, alpha=-self.lr)


def test_train(rank, net, results, start, args, epoch_counter,
               minibatch_counter, trainloader, train_sampler, testloader,
               best_acc, last_tested_at, process_barrier):
    torch.cuda.set_device(args.devicerank)
    if rank > 0:
        (trainloader, train_sampler, _), (testloader, _) = get_dataloader(args)
    print("LAPSGD Training Started at Commrank ", args.commrank, rank)
    torch.set_num_threads(args.num_threads)

    criterion = nn.CrossEntropyLoss()
    optimizer = opt(list(net.parameters()),
                    lr=args.baseline_lr,
                    momentum=args.momentum,
                    weight_decay=args.weight_decay,
                    nesterov=args.nesterov)

    if args.scheduler_type == 'cosine':
        scheduler = CosineAnnealingLR(optimizer, args)
    else:
        scheduler = MultiStepLR(optimizer, args)
    epoch = 0
    test_results = []
    train_results = []
    process_barrier.wait()
    while epoch < args.epochs:
        with epoch_counter.get_lock():
            sampling_epoch = epoch_counter.value
            epoch_counter.value += 1
        epoch = train_epoch(rank, net, args, trainloader, optimizer, scheduler,
                            criterion, sampling_epoch, results, start,
                            minibatch_counter, train_sampler, last_tested_at,
                            testloader, best_acc, test_results, train_results)
    results.append({'tag': 'testresult' + str(rank), 'val': test_results})
    results.append({'tag': 'trainresult' + str(rank), 'val': train_results})
    print("LAPSGD Training completed at rank ", rank, args.commrank)


def train_epoch(rank, net, args, trainloader, optimizer, scheduler, criterion,
                sampling_epoch, results, start, minibatch_counter,
                train_sampler, last_tested_at, testloader, best_acc,
                test_results, train_results):
    losses = LocalMetric('Loss')
    top1 = LocalMetric('Acc@1')
    if train_sampler is not None:
        train_sampler.set_epoch(sampling_epoch)
    net.train()
    b = bar(args.trainloaderlength, 30)
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        if args.cuda:
            inputs, targets = inputs.to('cuda'), targets.to('cuda')
        batch_loss = 0
        batch_acc = 0
        gr = None
        for i in range(0, len(inputs), args.train_processing_bs):
            net.train()
            data_batch = inputs[i:i + args.train_processing_bs]
            target_batch = targets[i:i + args.train_processing_bs]
            outputs = net(data_batch)
            loss = criterion(outputs, target_batch)
            acc = accuracy(outputs, target_batch)
            top1.update(acc.item(), data_batch.size(0))
            losses.update(loss.item(), data_batch.size(0))
            batch_loss += loss.item() * data_batch.size(0)
            batch_acc += acc.item() * data_batch.size(0)
            if len(inputs) != args.train_processing_bs:
                loss.div_(
                    math.ceil(float(len(inputs)) / args.train_processing_bs))
            grad = torch.autograd.grad(loss, optimizer.parameters)
            '''Gradient Accumulation'''
            if gr is None:
                gr = grad
            else:
                for g1, g2 in zip(gr, grad):
                    g1.add_(g2)
        '''Model update with gradient.'''
        with minibatch_counter.get_lock():
            minibatches = minibatch_counter.value
            with last_tested_at.get_lock():
                if minibatches - last_tested_at.value >= args.test_freq * args.trainloaderlength:
                    do_test = True
                    last_tested_at.value = minibatches
                else:
                    do_test = False
            minibatch_counter.value += 1
        epoch = minibatches * 1.0 / args.trainloaderlength
        scheduler.step(epoch)
        optimizer.step(gr)

        lr = optimizer.lr
        rightnow = time.perf_counter() - start
        banner_string = 'PID: {:d},CommRank: {:d}, Rank: {:d}|TrEp: {:.2f}|Loss: {:.4f}|Acc: {:4.3f}% ({:.0f}/{:.0f})|LR: {:.7f}'.format(
            os.getpid(), args.commrank, rank, epoch, losses.avg,
            top1.avg * 100, top1.sum, top1.count, lr)
        b.progress_bar(batch_idx, rightnow, banner_string)
        train_results.append(
            (minibatches, rightnow, batch_loss, batch_acc, len(inputs)))
        if do_test:
            test_epoch(net, args, start, testloader, criterion, best_acc,
                       test_results, epoch, rank)
        if minibatches >= args.epochs * args.trainloaderlength:
            break

    results.append({'tag': 'LR', 'ep': epoch, 'val': lr, 'time': rightnow})
    return epoch


def run(args):
    args.devicerank = args.commrank % torch.cuda.device_count()
    print("CommRank=", args.commrank, "CommSize=", args.commsize, "DeviceRank=", \
    args.devicerank,  args.dist_url, args.dist_backend)
    mp.set_start_method('spawn', force=True)
    torch.cuda.set_device(args.devicerank)
    dist.init_process_group(backend=args.dist_backend,
                            init_method=args.dist_url,
                            world_size=args.commsize,
                            rank=args.commrank)
    cudnn.deterministic = True
    cudnn.benchmark = False
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    net = get_model(args)
    net = net.cuda()
    start = time.perf_counter()
    manager = mp.Manager()
    results = manager.list()
    results.append({
        'tag': 'LR',
        'ep': 0,
        'val': args.baseline_lr,
        'time': time.perf_counter() - start
    })
    epoch_counter = mp.Value('i', 0)
    minibatch_counter = mp.Value('i', 0)
    last_tested_at = mp.Value('i', 0)
    best_acc = mp.Value('d', 0)
    process_barrier = mp.Barrier(args.num_processes)
    (trainloader, train_sampler, _), (testloader, _) = get_dataloader(args)
    args.trainloaderlength = len(trainloader)
    args.testloaderlength = len(testloader)
    processes = []
    for rank in range(args.num_processes):
        p = mp.Process(target=test_train,
                       args=(rank, net, results, start, args, epoch_counter,
                             minibatch_counter, trainloader, train_sampler,
                             testloader, best_acc, last_tested_at,
                             process_barrier))
        processes.append(p)
    for p in processes:
        p.start()
    average(net, args, minibatch_counter)
    for p in processes:
        p.join()
    process_test_result(results, args)
    process_train_result(results, args)
    results_summary(results, args)
    if args.storeresults:
        result_save(results, args)
    print("Run Complete!")
