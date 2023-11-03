torchrun --role $(hostname -s): --tee 3 --nnodes 1 --nproc-per-node=4 \
	train.py \
  --eval_every_steps 10 \
  --save_every_steps 10 \
  --per_device_batch_size 2 \
  --use_packed_sampler \
  --learning_rate 1e-5 \
  --max_eval_ids 200 \
  --seed 102 \
  --use_flash_attn \
  --model_name_or_path "../fuyu-tiny" \
#--model_name_or_path "../fuyu-2b" \
#  --model_name_or_path "fuyu-8b-slim-vocab" \
