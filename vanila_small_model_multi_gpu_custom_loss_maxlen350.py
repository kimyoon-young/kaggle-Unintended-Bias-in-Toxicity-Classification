from __future__ import absolute_import
from __future__ import division
from __future__ import print_function



# Version 2 + Bug fix - thanks to @chinhuic

# This Python 3 environment comes with many helpful analytics libraries installed
# It is defined by the kaggle/python docker image: https://github.com/kaggle/docker-python
# For example, here's several helpful packages to load in

import numpy as np # linear algebra
import pandas as pd # data processing, CSV file I/O (e.g. pd.read_csv)

# Input data files are available in the "../input/" directory.
# For example, running this (by clicking run or pressing Shift+Enter) will list the files in the input directory

import os
print(os.listdir("../input/nvidiaapex/repository/NVIDIA-apex-39e153a"))
#print(os.listdir("../input/glove-global-vectors-for-word-representation"))
#print(os.listdir("../input/jigsaw-unintended-bias-in-toxicity-classification"))
#print(os.listdir("../input/fas}ttext-crawl-300d-2m"))

# Any results you write to the current directory are saved as output.



import datetime
import pkg_resources
import seaborn as sns
import time
import scipy.stats as stats
import gc
import re
import operator
import sys
from sklearn import metrics
from sklearn import model_selection
import torch
import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F
from nltk.stem import PorterStemmer
from sklearn.metrics import roc_auc_score

#data paralle imprort
#from parallel import DataParallelModel, DataParallelCriterion


from tqdm import tqdm
import os
from IPython.core.interactiveshell import InteractiveShell
InteractiveShell.ast_node_interactivity = "all"
import warnings
warnings.filterwarnings(action='once')
import pickle
from apex import amp
import shutil
import time

#device=torch.device('cuda')
device = torch.device('cuda')


MAX_SEQUENCE_LENGTH = 330
SEED = 1245
EPOCHS = 4
Data_dir="../input/jigsaw-unintended-bias-in-toxicity-classification"
Input_dir = "../input"
WORK_DIR = "../working/"
num_to_load=1700000                         #Train size to match time limit
valid_size= 100000                       #Validation Size
TOXICITY_COLUMN = 'target'

# Add the Bart Pytorch repo to the PATH
# using files from: https://github.com/huggingface/pytorch-pretrained-BERT
package_dir_a = "../input/ppbert/pytorch-pretrained-bert/pytorch-pretrained-BERT"
sys.path.insert(0, package_dir_a)

from pytorch_pretrained_bert import convert_tf_checkpoint_to_pytorch
from pytorch_pretrained_bert import BertTokenizer, BertForSequenceClassification,BertAdam


# Translate model from tensorflow to pytorch
BERT_MODEL_PATH = '../input/bert-pretrained-models/uncased_l-12_h-768_a-12/uncased_L-12_H-768_A-12/'
convert_tf_checkpoint_to_pytorch.convert_tf_checkpoint_to_pytorch(
    BERT_MODEL_PATH + 'bert_model.ckpt',
BERT_MODEL_PATH + 'bert_config.json',
WORK_DIR + 'pytorch_model.bin')

shutil.copyfile(BERT_MODEL_PATH + 'bert_config.json', WORK_DIR + 'bert_config.json')

os.listdir("../working")


# This is the Bert configuration file
from pytorch_pretrained_bert import BertConfig

bert_config = BertConfig('../input/bert-pretrained-models/uncased_l-12_h-768_a-12/uncased_L-12_H-768_A-12/'+'bert_config.json')


# Converting the lines to BERT format
# Thanks to https://www.kaggle.com/httpwwwfszyc/bert-in-keras-taming
def convert_lines(example, max_seq_length,tokenizer):
    max_seq_length -=2
    all_tokens = []
    longer = 0
    for text in tqdm(example):
        tokens_a = tokenizer.tokenize(text)
        if len(tokens_a)>max_seq_length:
            tokens_a = tokens_a[:max_seq_length]
            longer += 1
        one_token = tokenizer.convert_tokens_to_ids(["[CLS]"]+tokens_a+["[SEP]"])+[0] * (max_seq_length - len(tokens_a))
        all_tokens.append(one_token)
    print(longer)
    return np.array(all_tokens)


#valid test
####
SUBGROUP_AUC = 'subgroup_auc'
BPSN_AUC = 'bpsn_auc'  # stands for background positive, subgroup negative
BNSP_AUC = 'bnsp_auc'  # stands for background negative, subgroup positive



# From baseline kernel

def calculate_overall_auc(df, model_name):
    true_labels = df[TOXICITY_COLUMN]>0.5
    predicted_labels = df[model_name]
    return metrics.roc_auc_score(true_labels, predicted_labels)

def power_mean(series, p):
    total = sum(np.power(series, p))
    return np.power(total / len(series), 1 / p)

def get_final_metric(bias_df, overall_auc, POWER=-5, OVERALL_MODEL_WEIGHT=0.25):
    bias_score = np.average([
        power_mean(bias_df[SUBGROUP_AUC], POWER),
        power_mean(bias_df[BPSN_AUC], POWER),
        power_mean(bias_df[BNSP_AUC], POWER)
    ])
    return (OVERALL_MODEL_WEIGHT * overall_auc) + ((1 - OVERALL_MODEL_WEIGHT) * bias_score)



def compute_auc(y_true, y_pred):
    try:
        return metrics.roc_auc_score(y_true, y_pred)
    except ValueError:
        return np.nan

def compute_subgroup_auc(df, subgroup, label, model_name):
    subgroup_examples = df[df[subgroup]>0.5]
    return compute_auc((subgroup_examples[label]>0.5), subgroup_examples[model_name])

def compute_bpsn_auc(df, subgroup, label, model_name):
    """Computes the AUC of the within-subgroup negative examples and the background positive examples."""
    subgroup_negative_examples = df[(df[subgroup]>0.5) & (df[label]<=0.5)]
    non_subgroup_positive_examples = df[(df[subgroup]<=0.5) & (df[label]>0.5)]
    examples = subgroup_negative_examples.append(non_subgroup_positive_examples)
    return compute_auc(examples[label]>0.5, examples[model_name])

def compute_bnsp_auc(df, subgroup, label, model_name):
    """Computes the AUC of the within-subgroup positive examples and the background negative examples."""
    subgroup_positive_examples = df[(df[subgroup]>0.5) & (df[label]>0.5)]
    non_subgroup_negative_examples = df[(df[subgroup]<=0.5) & (df[label]<=0.5)]
    examples = subgroup_positive_examples.append(non_subgroup_negative_examples)
    return compute_auc(examples[label]>0.5, examples[model_name])

def compute_bias_metrics_for_model(dataset,
                                   subgroups,
                                   model,
                                   label_col,
                                   include_asegs=False):
    """Computes per-subgroup metrics for all subgroups and one model."""
    records = []
    for subgroup in subgroups:
        record = {
            'subgroup': subgroup,
            'subgroup_size': len(dataset[dataset[subgroup]>0.5])
        }
        record[SUBGROUP_AUC] = compute_subgroup_auc(dataset, subgroup, label_col, model)
        record[BPSN_AUC] = compute_bpsn_auc(dataset, subgroup, label_col, model)
        record[BNSP_AUC] = compute_bnsp_auc(dataset, subgroup, label_col, model)
        records.append(record)
    return pd.DataFrame(records).sort_values('subgroup_auc', ascending=True)







BERT_MODEL_PATH = '../input/bert-pretrained-models/uncased_l-12_h-768_a-12/uncased_L-12_H-768_A-12/'



tokenizer = BertTokenizer.from_pretrained(BERT_MODEL_PATH, cache_dir=None,do_lower_case=True)
train_df = pd.read_csv(os.path.join(Data_dir,"train.csv")).sample(num_to_load+valid_size,random_state=SEED)

train_only_df = train_df.iloc[:num_to_load,:]
print('loaded %d records' % len(train_df))

# Make sure all comment_text values are strings
train_df['comment_text'] = train_df['comment_text'].astype(str)

## for custom loss


identity_columns = [
    'male', 'female', 'homosexual_gay_or_lesbian', 'christian', 'jewish',
    'muslim', 'black', 'white', 'psychiatric_or_mental_illness']
# Overall
weights = np.ones((num_to_load,)) / 4

print(weights.shape)
# Subgroup
weights += (train_only_df[identity_columns].fillna(0).values>=0.5).sum(axis=1).astype(bool).astype(np.int) / 4
# Background Positive, Subgroup Negative
weights += (( (train_only_df['target'].values>=0.5).astype(bool).astype(np.int) +
   (train_only_df[identity_columns].fillna(0).values<0.5).sum(axis=1).astype(bool).astype(np.int) ) > 1 ).astype(bool).astype(np.int) / 4
# Background Negative, Subgroup Positive
weights += (( (train_only_df['target'].values<0.5).astype(bool).astype(np.int) +
   (train_only_df[identity_columns].fillna(0).values>=0.5).sum(axis=1).astype(bool).astype(np.int) ) > 1 ).astype(bool).astype(np.int) / 4
loss_weight = 1.0 / weights.mean()

##



sequences = convert_lines(train_df["comment_text"].fillna("DUMMY_VALUE"),MAX_SEQUENCE_LENGTH,tokenizer)
train_df=train_df.fillna(0)
# List all identities
identity_columns = [
    'male', 'female', 'homosexual_gay_or_lesbian', 'christian', 'jewish',
    'muslim', 'black', 'white', 'psychiatric_or_mental_illness']
y_columns=['target']

train_df = train_df.drop(['comment_text'],axis=1)
# convert target to 0,1
train_df['target']=(train_df['target']>=0.5).astype(float)





X = sequences[:num_to_load]
y = train_df[y_columns].values[:num_to_load]
y = np.reshape(y, (num_to_load,))

print('y : {}'.format(y.shape))
print('w : {}'.format(weights.shape))

y = np.vstack([y,weights]).T



X_val = sequences[num_to_load:]
y_val = train_df[y_columns].values[num_to_load:]



test_df=train_df.tail(valid_size).copy()
train_df=train_df.head(num_to_load)




train_dataset = torch.utils.data.TensorDataset(torch.tensor(X,dtype=torch.long), torch.tensor(y,dtype=torch.float))



output_model_file = "small_bert_pytorch_170mi_330maxlen_lr2e5_batch64_custom_loss"

lr=2e-5
batch_size = 64
accumulation_steps=2
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.deterministic = True

model = BertForSequenceClassification.from_pretrained("../working",cache_dir=None,num_labels=len(y_columns))


model.zero_grad()
model = model.to(device)
param_optimizer = list(model.named_parameters())
no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
train = train_dataset

num_train_optimization_steps = int(EPOCHS*len(train)/batch_size/accumulation_steps)

optimizer = BertAdam(optimizer_grouped_parameters,
                     lr=lr,
                     warmup=0.05,
                     t_total=num_train_optimization_steps)

model, optimizer = amp.initialize(model, optimizer, opt_level="O1",verbosity=0)

#paralleism
model = nn.DataParallel(model)


model=model.train()


## use custom_loss

def custom_loss(data, targets):
    ''' Define custom loss function for weighted BCE on 'target' column '''
    bce_loss_1 = nn.BCEWithLogitsLoss(weight=targets[:,1:2])(data,targets[:,:1])
    #bce_loss_2 = nn.BCEWithLogitsLoss()(data[:,1:],targets[:,2:])
    return (bce_loss_1 * loss_weight)
    #return (bce_loss_1 * loss_weight) + bce_loss_2







tq = tqdm(range(EPOCHS))
for epoch in tq:
    train_loader = torch.utils.data.DataLoader(train, batch_size=batch_size, shuffle=True)
    avg_loss = 0.
    avg_accuracy = 0.
    lossf=None
    tk0 = tqdm(enumerate(train_loader),total=len(train_loader),leave=False)
    optimizer.zero_grad()   # Bug fix - thanks to @chinhuic
    for i,(x_batch, y_batch) in tk0:
#        optimizer.zero_grad()
        y_pred = model(x_batch.to(device), attention_mask=(x_batch>0).to(device), labels=None)

     
        #loss =  F.binary_cross_entropy_with_logits(y_pred,y_batch.to(device))
        loss = custom_loss(y_pred,y_batch.to(device))




        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()
        if (i+1) % accumulation_steps == 0:             # Wait for several backward steps
            optimizer.step()                            # Now we can do an optimizer step
            optimizer.zero_grad()
        if lossf:
            lossf = 0.98*lossf+0.02*loss.item()
        else:
            lossf = loss.item()
        tk0.set_postfix(loss = lossf)
        avg_loss += loss.item() / len(train_loader)
        avg_accuracy += torch.mean(((torch.sigmoid(y_pred[:,0])>0.5) == (y_batch[:,0]>0.5).to(device)).to(torch.float) ).item()/len(train_loader)
    tq.set_postfix(avg_loss=avg_loss,avg_accuracy=avg_accuracy)
    torch.save(model.state_dict(),  output_model_file  + '_epoch_' + str(epoch) + '.bin')

    #validate
    test_model = BertForSequenceClassification(bert_config,num_labels=len(y_columns))

    #paralleism
    test_model = nn.DataParallel(test_model)

    test_model.load_state_dict(torch.load(output_model_file  + '_epoch_' + str(epoch) + '.bin'))
    test_model.to(device)
    for param in test_model.parameters():
        param.requires_grad=False
    test_model.eval()
    valid_preds = np.zeros((len(X_val)))
    print(valid_preds.size)
    valid = torch.utils.data.TensorDataset(torch.tensor(X_val,dtype=torch.long))
    valid_loader = torch.utils.data.DataLoader(valid, batch_size=256, shuffle=False)

    tk0 = tqdm(valid_loader)
    for i,(x_batch,)  in enumerate(tk0):
        pred = test_model(x_batch.to(device), attention_mask=(x_batch>0).to(device), labels=None)
        valid_preds[i*256:(i+1)*256]=pred[:,0].detach().cpu().squeeze().numpy()

    MODEL_NAME = 'model1'
    test_df[MODEL_NAME] = torch.sigmoid(torch.tensor(valid_preds)).numpy()
    TOXICITY_COLUMN = 'target'
    bias_metrics_df = compute_bias_metrics_for_model(test_df, identity_columns, MODEL_NAME, 'target')
    print(bias_metrics_df)
    print(get_final_metric(bias_metrics_df, calculate_overall_auc(test_df, MODEL_NAME)))





