"""
This training script can be run both on a single gpu in debug mode,
and also in a larger training run with distributed data parallel (ddp).

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False

To run with DDP on 4 gpus on 1 node, example:
$ torchrun --standalone --nproc_per_node=4 train.py

To run with DDP on 4 gpus across 2 nodes, example:
- Run on the first (master) node with example IP 123.456.123.456:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
- Run on the worker node:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
(If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
"""

import os
import time
import datetime
import math
from contextlib import nullcontext
import torch
import torch.distributed
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
import torch.distributed as dist
from model import GPTConfig, GPT, CombinedModel
from torchvision import models
from encoder import PositionalEncoding2D, InputEmbeddings
from torchvision.models import DenseNet169_Weights
from dataloader import CustomDataLoader, CustomDataset, SubsetCustomDataLoader, dist_sampler
import wandb
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

# logging
log_file = 'training_log.txt'
sample_interval = 100  # Log sample predictions every 100 iterations
# I/O
out_dir = 'out'
eval_interval = 600
log_interval = 50
eval_iters = 355
eval_only = False # if True, script exits right after the first eval
always_save_checkpoint = False # if True, always save a checkpoint after each eval
init_from = 'scratch' # 'scratch' or 'resume' or 'gpt2*'
# wandb logging
wandb_log = True # disabled by default
wandb_project = 'image2latex'
wandb_run_name = 'run' + str(time.time())
gradient_accumulation_steps = 8 #8*4 for 8 GPUs # used to simulate larger batch sizes
batch_size = 8   # if gradient_accumulation_steps > 1, this is the MICRO-BATCH SIZE
block_size = 300 # max token length
# model
n_layer = 12
n_head = 8
n_embd = 512
dropout = 0.0 # for pretraining 0 is good, for finetuning try 0.1+
bias = False # do we use bias inside LayerNorm and Linear layers?
# adamw optimizer
learning_rate = 6e-4 # max learning rate
max_iters = 120000 # total number of training iterations
weight_decay = 1e-1
beta1 = 0.9
beta2 = 0.95
grad_clip = 1.0 # clip gradients at this value, or disable if == 0.0
# learning rate decay settings
decay_lr = True # whether to decay the learning rate
warmup_iters = 2000 # how many steps to warm up for
lr_decay_iters = 45000 # should be ~= max_iters per Chinchilla
min_lr = 6e-5 # minimum learning rate, should be ~= learning_rate/10 per Chinchilla
# DDP settings
backend = 'nccl' # 'nccl', 'gloo', etc.
# system
device = 'cuda' if torch.cuda.is_available() else 'cpu' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16' # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
compile = False # use PyTorch 2.0 to compile the model to be faster
# init these up here, can override if init_from='resume' (i.e. from a checkpoint)
iter_num = 0
best_val_loss = 1e9
best_val_bleu = 0.0
num_workers = 0 # number of DataLoader workers
num_epochs = 100
max_length = 300
max_n = 4 # max n-gram for BLEU score
subset_size = 300000

# configuration parameters that are allowed to be overridden from command line
config_keys = [k for k,v in globals().items() if not k.startswith('_') and isinstance(v, (int, float, bool, str))]
exec(open('configurator.py').read()) # overrides from command line or config file
config = {k: globals()[k] for k in config_keys} # will be useful for logging


# setting up the environment for distributed data parallel training 
# various inits, derived attributes, I/O setup
ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
if ddp:
    # set master address & master port
    # MASTER_ADDR is the IP address of the machine running the rank 0 process
    os.environ['MASTER_ADDR'] = os.environ.get('MASTER_ADDR', 'localhost')
    os.environ['MASTER_PORT'] = os.environ.get('MASTER_PORT', '12355')
    init_process_group(backend=backend)
    ddp_rank = int(os.environ['RANK']) # rank refers to the unique identifier assigned to each process in the group
    ddp_local_rank = int(os.environ['LOCAL_RANK']) 
    ddp_world_size = int(os.environ['WORLD_SIZE']) # world_size refers to the total number of processes in the group
    device = f'cuda:{ddp_local_rank}'
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0 # this process will do logging, checkpointing etc.
    seed_offset = ddp_rank # each process gets a different seed
    # world_size number of processes will be training simultaneously, so we can scale
    # down the desired gradient accumulation iterations per process proportionally
    assert gradient_accumulation_steps % ddp_world_size == 0
    gradient_accumulation_steps //= ddp_world_size
else:
    # if not ddp, we are running on a single gpu, and one process
    master_process = True
    seed_offset = 0
    ddp_world_size = 1
tokens_per_iter = gradient_accumulation_steps * ddp_world_size * batch_size * block_size
if master_process:
    print(f"tokens per iteration will be: {tokens_per_iter:,}")

if master_process:
    os.makedirs(out_dir, exist_ok=True)
torch.manual_seed(1337 + seed_offset)
torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
# note: float16 data type will automatically use a GradScaler
ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype) # context managaer for auto mixed precision training


# model init
model_args = dict(n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=300,
                  bias=bias, dropout=dropout) # start with model_args from command line

# Define the DenseNet169 model
densenet_model = models.densenet169(weights=DenseNet169_Weights.IMAGENET1K_V1) # change to DEFAULT HERE
# Remove the final fully connected layer to get the final feature maps
densenet_model = nn.Sequential(*list(densenet_model.children())[:-1])
densenet_model.add_module('PositionalEncoding2D', PositionalEncoding2D(1664, 12, 25)) # hardcoded this based on denseNet output size
densenet_model.add_module('InputEmbeddings', InputEmbeddings(1664, GPTConfig.n_embd))
# Move the DenseNet model to the correct device
densenet_model = densenet_model.to(device)

# Define the GPT model
gptconf = GPTConfig(**model_args)
gpt_model = GPT(gptconf)

# Combine the DenseNet and GPT models
model = CombinedModel(densenet_model, gpt_model)

# initialization of model based on the arg init_from (resume, scratch, gpt2, etc.)
if init_from == 'scratch':
    if master_process :
        # init a new model from scratch
        print("Initializing a new model from scratch")


elif init_from == 'resume':
    if master_process:
        # load the model from the last
        print(f"Resuming training from {out_dir}")
    ckpt_path = os.path.join(out_dir, 'best_model.pt')
    checkpoint = torch.load(ckpt_path, map_location=device)
    
    # Load the state dict
    model.load_state_dict(checkpoint['model'])
    
    # Load other training state
    iter_num = checkpoint['iter_num']
    best_val_loss = checkpoint['best_val_loss']
    best_val_bleu = checkpoint['best_val_bleu']

    # Broadcast iter_num and best_val_loss
    if ddp:
        iter_num = torch.tensor(iter_num).to(device)
        best_val_loss = torch.tensor(best_val_loss).to(device)
        dist.broadcast(iter_num, src=0)
        dist.broadcast(best_val_loss, src=0)
        iter_num = iter_num.item()
        best_val_loss = best_val_loss.item()

# crop down the model block size if desired, using model surgery
if block_size < model.original_model.config.block_size:
    model.original_model.crop_block_size(block_size)
    model_args['block_size'] = block_size


# move the model to the correct device
model.to(device)

# initialize a GradScaler. If enabled=False scaler is a no-op
scaler = torch.cuda.amp.GradScaler(enabled=(dtype == 'float16'))

# optimizer
optimizer = gpt_model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
if init_from == 'resume':
    optimizer.load_state_dict(checkpoint['optimizer'])
checkpoint = None # free up memory

# compile the model
# if compile:
#     print("compiling the model... (takes a ~minute)")
#     unoptimized_model = gpt_model
#     model = torch.compile(model) # requires PyTorch 2.0

# wrap model into DDP container
if ddp:
    model = DDP(model, device_ids=[ddp_local_rank], find_unused_parameters=True)


# Load the tokenizer
tokenizer = gpt_model.tokenizer

# logging results to a txt file
def log_info(message, also_print=False):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}"
    with open(log_file, 'a', encoding="utf-8") as f:
        f.write(log_message + '\n')
    if also_print:
        print(log_message)


if ddp:
    train_sampler, val_sampler = dist_sampler(ddp, ddp_rank, ddp_world_size)
else:
    train_sampler, val_sampler = None, None
            

# get the dataloader
# train_loader = CustomDataLoader(batch_size=batch_size, image_dir='./data/UniMER-1M/images', label_file='./data/UniMER-1M/train.txt', 
#                                 process_rank=ddp_rank if ddp else 0,
#                                 num_processes=ddp_world_size if ddp else 1,
#                                 num_workers=num_workers, sampler=train_sampler)

# val_loader = CustomDataLoader(batch_size=batch_size, image_dir='./data/UniMER-Test/spe/', label_file='./data/UniMER-Test/spe.txt', cache_file='valid_indices_val.pkl', 
#                               process_rank=ddp_rank if ddp else 0,
#                               num_processes=ddp_world_size if ddp else 1,
#                               num_workers=num_workers, sampler=val_sampler)


# get subset dataloader
train_loader = SubsetCustomDataLoader(batch_size=batch_size, image_dir='./data/UniMER-1M/images', label_file='./data/UniMER-1M/train.txt', 
                                        subset_size=subset_size,
                                        process_rank=ddp_rank if ddp else 0,
                                        num_processes=ddp_world_size if ddp else 1,
                                        num_workers=num_workers, sampler=train_sampler)

val_loader = CustomDataLoader(batch_size=batch_size, image_dir='./data/UniMER-Test/spe/', label_file='./data/UniMER-Test/spe.txt', cache_file='valid_indices_val.pkl', 
                              process_rank=ddp_rank if ddp else 0,
                              num_processes=ddp_world_size if ddp else 1,
                              num_workers=num_workers, sampler=val_sampler)


def get_lr(it):
    """Learning rate decay scheduler w cosine warmup"""
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * it / warmup_iters
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)

# logging
if wandb_log and master_process:
    wandb.init(project=wandb_project, name=wandb_run_name, config=config)
else :
    wandb_log = False

t0 = time.time()
# set the model to training mode
for param in model.parameters():
    param.requires_grad = True

# training looop
for epoch in range(num_epochs):

    if ddp :
        train_loader.sampler.set_epoch(epoch)
        val_loader.sampler.set_epoch(epoch)
    
    model.train()

    if master_process:
        log_info(f"Starting epoch {epoch+1}/{num_epochs}", also_print=True)
    
    for batch_idx, (images, latex_labels) in enumerate(train_loader):

        # Get the image embeddings and the latex labels
        images = images.to(device)
        
        # Tokenize LaTeX labels
        input_ids, attention_mask, targets = model.module.tokenize_latex(latex_labels, tokenizer=tokenizer, max_length=300)
        input_ids, attention_mask, targets = input_ids.to(device), attention_mask.to(device), targets.to(device)
        
        # Determine and set the learning rate for this iteration
        lr = get_lr(iter_num) if decay_lr else learning_rate
        # update the learning rate
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # Evaluation and checkpointing
        if iter_num % eval_interval == 0 and master_process:
            with torch.no_grad():
                if ddp :
                    val_loss, val_bleu = model.module.evaluate(model, val_loader, device=device, eval_iters=eval_iters, 
                                                                       gradient_accumulation_steps=gradient_accumulation_steps, max_n=max_n, tokenizer=tokenizer)
                else :
                    val_loss, val_bleu  = model.evaluate(model, val_loader, device=device, eval_iters=eval_iters, 
                                                                gradient_accumulation_steps=gradient_accumulation_steps, max_n=max_n, tokenizer=tokenizer)

                print(f"step {iter_num} | val loss {val_loss:.4f} | val BLEU {val_bleu:.4f}")

                if wandb_log:
                    wandb.log({
                        "iter": iter_num,
                        "val/loss": val_loss,
                        "lr": lr,
                        "val/bleu": val_bleu,
                        # "val/ppl": math.exp(val_loss),
                    }, step = iter_num)

                # save the model if its the best so far
                if val_loss < best_val_loss or always_save_checkpoint or val_bleu > best_val_bleu:
                    if val_loss < best_val_loss:
                        best_val_loss = val_loss
                    
                    elif val_bleu > best_val_bleu:
                        best_val_bleu = val_bleu
                    

                    if iter_num > 0:
                        checkpoint = {
                            'model': model.module.state_dict() if ddp else model.state_dict(),
                            'optimizer': optimizer.state_dict(),
                            'model_args': model_args,
                            'best_val_loss': best_val_loss,
                            'config': config,
                            'best_val_bleu': best_val_bleu,
                        }
                        print(f"saving checkpoint to {out_dir}")
                        torch.save(checkpoint, os.path.join(out_dir, f'best_model.pt'))

        if iter_num == 0 and eval_only:
            break

        # Forward backward update, with optional gradient accumulation
        for micro_step in range(gradient_accumulation_steps):
            if ddp:
                model.require_backward_grad_sync = (micro_step == gradient_accumulation_steps - 1)
            
            # Forward pass
            with ctx:
                outputs = model(images=images, targets=targets)
                if isinstance(outputs, tuple):
                    logits, loss = outputs
                else :
                    logits, loss = outputs, None

                # Ensure loss is a tensor
                if not isinstance(loss, torch.Tensor):
                    loss = torch.tensor(loss, requires_grad=True)
                # Get the predicted tokens
                sample_prediction = torch.multinomial(logits[0].softmax(dim=-1), num_samples=1)
                non_pad_mask = sample_prediction != tokenizer.pad_token_id
                decoded_prediction = tokenizer.decode(sample_prediction[non_pad_mask])
                orig_loss = outputs[1]

                # for backward pass not logging
                loss = outputs[1] / gradient_accumulation_steps
                loss = loss.to(device)

                # for ddp
                if ddp :
                    loss_tensor = orig_loss.clone()
                    # perform all_reduce
                    dist.all_reduce(loss_tensor, op=dist.ReduceOp.AVG) # averaging loss across multiple GPUs
                    # update the loss
                    avg_loss = loss_tensor

                if wandb_log and master_process:
                    wandb.log({
                        "train/loss": avg_loss.item() if ddp else orig_loss.item(),
                        # "train/ppl": math.exp(avg_loss.item() if ddp else orig_loss.item()),
                    }, step = iter_num)

            # Backward pass
            scaler.scale(loss).backward()
        
        # Clip the gradient
        if grad_clip != 0.0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
        
        # Step the optimizer and scaler if training in fp16
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)

        # Timing and logging
        t1 = time.time()
        dt = t1 - t0
        t0 = t1

        if iter_num % log_interval == 0 and master_process:
            # get loss as float. note: this is a CPU-GPU sync point
            # scale up to undo the division above, approximating the true total loss (exact would have been a sum)
            lossf = loss.item() * gradient_accumulation_steps

            log_message = f"Epoch {epoch+1} | iter {iter_num} : loss {lossf:.4f} | time {dt*1000:.2f}ms | lr {lr:.6f} | tok/s {tokens_per_iter/dt:.2f}"
            print(log_message)


        # Log sample predictions
        if iter_num % sample_interval == 0:
            log_info(f" prediction at iter {iter_num} |  pred : {decoded_prediction}")
            log_info(f" label : {latex_labels[0]}")            

        iter_num += 1

    if ddp :
        torch.distributed.barrier() # sync
        
        # termination condition
        if iter_num > max_iters:
            log_info(f"Reached maximum iterations ({max_iters}). Stopping training.", also_print=True)
            break

print(f"Training completed. Total iterations: {iter_num}")

if ddp:
    destroy_process_group()