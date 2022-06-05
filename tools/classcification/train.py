import os
import time
import logging
import argparse
import warnings

warnings.filterwarnings('ignore')

import torch
import torchvision
import torch.nn as nn
import torch.nn.parallel
import torch.optim as optim
from torchvision import transforms
from torch.utils.data import DataLoader
from models.vgg16 import vgg16

from utils.path import *
from utils.dataloader import pets
from utils.accuracy import accuracy
from utils.collate import collate
from utils.get_logger import get_logger
from utils.AverageMeter import AverageMeter


def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch Detection Training')
    parser.add_mutually_exclusive_group()
    parser.add_argument('--dataset',
                        type=str,
                        default='pets',
                        choices=['CIFAR10', 'pets'],
                        help=' CIFAR10 or pets')
    parser.add_argument('--dataset_root',
                        type=str,
                        default=PETS_path,
                        choices=[CIFAR_path, PETS_path],
                        help='Dataset root directory path')
    parser.add_argument('--basenet',
                        type=str,
                        default='vgg',
                        help='Pretrained base model')
    parser.add_argument('--depth',
                        type=int,
                        default=16,
                        help='Basenet depth')
    parser.add_argument('--batch_size',
                        type=int,
                        default=64,
                        help='Batch size for training')
    parser.add_argument('--pretrained',
                        type=str,
                        default=True,
                        help='Checkpoint state_dict file to resume training from')
    parser.add_argument('--num_workers',
                        type=int,
                        default=0,
                        help='Number of workers user in dataloading')
    parser.add_argument('--cuda',
                        type=str,
                        default=True,
                        help='Use CUDA to train model')
    parser.add_argument('--save_folder',
                        type=str,
                        default=CheckPoints,
                        help='Directory for saving checkpoint models')
    parser.add_argument('--tensorboard',
                        type=str,
                        default=False,
                        help='Use tensorboard for loss visualization')
    parser.add_argument('--log_folder',
                        type=str,
                        default=log,
                        help='Log Folder')
    parser.add_argument('--log_name',
                        type=str,
                        default=classification_train_log,
                        help='Log Name')
    parser.add_argument('--tensorboard_log',
                        type=str,
                        default=tensorboard_log,
                        help='Use tensorboard for loss visualization')
    parser.add_argument('--lr',
                        type=float,
                        default=1e-3,
                        help='learning rate')
    parser.add_argument('--epochs',
                        type=int,
                        default=200,
                        help='Number of epochs')
    parser.add_argument('--num_classes',
                        type=int,
                        default=2,
                        help='the number classes')
    parser.add_argument('--image_size',
                        type=int,
                        default=64,
                        help='image size')
    parser.add_argument('--accumulation_steps',
                        type=int,
                        default=1,
                        help='Gradient acumulation steps')
    parser.add_argument('--resume',
                        type=str,
                        default=None,
                        help='Checkpoint state_dict file to resume training from')

    return parser.parse_args()


args = parse_args()

# 1. Torch choose cuda or cpu
if torch.cuda.is_available():
    if args.cuda:
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not args.cuda:
        print("WARNING: It looks like you have a CUDA device, but you aren't using it" +
              "\n You can set the parameter of cuda to True.")
        torch.set_default_tensor_type('torch.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')

if os.path.exists(args.save_folder) is None:
    os.mkdir(args.save_folder)

# 2. Log
get_logger(args.log_folder, args.log_name)
logger = logging.getLogger(args.log_name)


def train():
    # 3. Create SummaryWriter
    if args.tensorboard:
        from torch.utils.tensorboard import SummaryWriter
        # tensorboard  loss
        writer = SummaryWriter(args.tensorboard_log)

    # 4. Ready dataset
    if args.dataset == 'CIFAR10':
        if args.dataset_root != CIFAR_path:
            raise ValueError('Must specify dataset_root if specifying dataset CIFAR10.')

        elif args.dataset_root is None:
            raise ValueError("Must provide --dataset_root when training on CIFAR10.")

        dataset = torchvision.datasets.CIFAR10(root=args.dataset_root, train=True,
                                               transform=torchvision.transforms.Compose([
                                                   transforms.Resize((args.image_size,
                                                                      args.image_size)),
                                                   torchvision.transforms.ToTensor()]))
    elif args.dataset == 'pets':
        if args.dataset_root != PETS_path:
            raise ValueError('Must specify dataset_root if specifying dataset PETS.')

        elif args.dataset_root is None:
            raise ValueError("Must provide --dataset_root when training on PETS.")

        dataset = pets(root_dir=args.dataset_root, transform=transforms.Compose([
            transforms.Resize((args.image_size, args.image_size)), transforms.ToTensor()]))

    dataloader = torch.utils.data.DataLoader(dataset=dataset,
                                             batch_size=args.batch_size,
                                             num_workers=args.num_workers,
                                             shuffle=True,
                                             collate_fn=collate)
    top1 = AverageMeter()
    losses = AverageMeter()

    # 5. Define train model
    if args.basenet == 'vgg':
        if args.depth == 16:
            model = vgg16(num_classes=args.num_classes, pretrained=args.pretrained)
        else:
            raise ValueError("Unsupported model depth!")
    else:
        raise ValueError('Unsupported model type!')

    if args.cuda:
        if torch.cuda.is_available():
            model = model.cuda()
            model = torch.nn.DataParallel(model).cuda()
    else:
        model = torch.nn.DataParallel(model)

    # 6. Loading weights
    if args.resume:
        other, ext = os.path.splitext(args.resume)
        if ext == '.pkl' or '.pth':
            print('Loading weights into state dict...')
            model_load = os.path.join(args.save_folder, args.resume)
            model.load_state_dict(torch.load(model_load))
        else:
            print('Sorry only .pth and .pkl files supported.')
    elif args.resume is None:
        print("Initializing weights...")
        # initialize newly added models' weights with xavier method
        model.apply(weights_init)

    model.train()

    iteration = 0

    # 7. Optimizer
    optimizer = optim.AdamW(model.parameters(), lr=args.lr)
    # optimizer = optim.AdamW(model.module.classifier.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, patience=3, verbose=True)

    # 8. Length
    iter_size = len(dataset) // args.batch_size
    print("len(dataset): {}, iter_size: {}".format(len(dataset), iter_size))
    logger.info(f"args - {args}")
    t0 = time.time()

    # 9. Create batch iterator
    for epoch in range(args.epochs):
        t1 = time.time()
        torch.cuda.empty_cache()
        # 10. Load train data
        for data in dataloader:
            iteration += 1
            images, targets = data
            if args.cuda:
                images, targets = images.cuda(), targets.cuda()

            optimizer.zero_grad()
            # 11. Forward
            outputs = model(images)
            if args.cuda:
                criterion = criterion.cuda()

            loss = criterion(outputs, targets.long())
            loss = loss / args.accumulation_steps

            # 12. Backward
            loss.backward()
            optimizer.step()

            # 13. Measure accuracy and record loss
            acc1, _ = accuracy(outputs, targets, topk=(1, 1))
            top1.update(acc1.item(), images.size(0))
            losses.update(loss.item(), images.size(0))

            if args.tensorboard:
                writer.add_scalar("train_classification_loss", loss.item(), iteration)

            if iteration % 1 == 0:
                logger.info(
                    f"- epoch: {epoch},  iteration: {iteration}, lr:{optimizer.param_groups[0]['lr']}, "
                    f"top1 acc: {acc1.item():.2f}% , "
                    f"loss: {loss.item():.3f}, (losses.avg): {losses.avg:3f} "
                )

        scheduler.step(losses.avg)

        t2 = time.time()
        h_time = (t2 - t1) // 3600
        m_time = ((t2 - t1) % 3600) // 60
        s_time = ((t2 - t1) % 3600) % 60
        print("epoch {} is finished, and the time is {}h{}min{}s".format(epoch, int(h_time), int(m_time), int(s_time)))

        # 14. Save train model
        if epoch != 0 and epoch % 5 == 0:
            print('Saving state, iter:', epoch)
            torch.save(model.state_dict(),
                       args.save_folder + '/' + args.dataset +
                       '_' + args.basenet + str(args.depth) + '_' + repr(epoch) + '.pth')
        torch.save(model.state_dict(),
                   args.save_folder + '/' + args.dataset + "_" + args.basenet + str(args.depth) + '.pth')

    if args.tensorboard:
        writer.close()

    t3 = time.time()
    h = (t3 - t0) // 3600
    m = ((t3 - t0) % 3600) // 60
    s = ((t3 - t0) % 3600) % 60
    print("The Finished Time is {}h{}m{}s".format(int(h), int(m), int(s)))
    return top1.avg, losses.avg


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif isinstance(m, nn.BatchNorm2d):
        nn.init.normal_(m.weight.data, 1.0, 0.02)
        nn.init.constant_(m.bias, 0.0)
    elif isinstance(m, nn.Linear):
        nn.init.kaiming_normal_(m.weight)
        nn.init.constant_(m.bias, 0.0)


if __name__ == '__main__':
    torch.multiprocessing.set_start_method('spawn')
    logger.info("Program started")
    top1, loss = train()
    print("top1 acc: {}, loss:{}".format(top1, loss))
    logger.info("Done!")
