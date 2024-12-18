import torch
import torch.nn as nn
from torch.nn import Parameter
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, SAGEConv, GATConv
import torch_sparse
from horology import Timing

def get_optimizer(args, paras):
    name = args.optimizer
    lr = args.lr
    if name == 'Adam':
        optimizer = torch.optim.Adam(paras, lr=lr)
    elif name == 'AdamW':
        optimizer = torch.optim.AdamW(paras, lr=lr)
    elif name == 'SGD':
        optimizer = torch.optim.SGD(paras, lr=lr, momentum=0.9, weight_decay=1e-5, nesterov=True)
    return optimizer


def SparseTensor_norm(mask, method: str = 'row_sum'):
    if isinstance(mask, torch_sparse.SparseTensor):
        if method == 'row_sum':
            deg_inv = 1 / (torch_sparse.sum(mask, dim=1) + 1e-15)
            # deg_inv.masked_fill_(deg_inv == float('inf'), 0.)
            mask = torch_sparse.mul(mask, deg_inv.view(-1, 1))
        elif method == 'symmetric':
            deg = torch_sparse.sum(mask, dim=1)
            deg_inv_sqrt = 1/(deg.pow_(0.5) + 1e-15)
            # deg_inv_sqrt.masked_fill_(deg_inv_sqrt == float('inf'), 0.)
            mask = torch_sparse.mul(mask, deg_inv_sqrt.view(-1, 1))
            mask = torch_sparse.mul(mask, deg_inv_sqrt.view(1, -1))
    return mask

def init_params(module):
    if isinstance(module, nn.Linear):
        nn.init.xavier_uniform_(module.weight)
        if module.bias is not None:
            nn.init.zeros_(module.bias)
    if isinstance(module, nn.Embedding):
        torch.nn.init.xavier_uniform_(module.weight)


class Predictor(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.dropout = args.dropout
        if self.args.use_node_emb:
            self.emb_node = nn.Embedding(args.num_nodes, args.dim_node_emb)
        if self.args.use_degree:
            self.emb_degree = nn.Embedding(args.max_degree + 1, args.dim_encoding)
        ## embedding huristics
        emb_heuristics = []
        self.num_heuristics = sum([args.use_dist, args.use_cn, args.use_ja, args.use_aa, args.use_ra])
        max_heuristics = max(args.max_dist,args.max_cn,args.max_ja,args.max_aa,args.max_ra)+1
        for i in range(self.num_heuristics):
            emb_heuristics.append(nn.Embedding(max_heuristics, args.dim_encoding))
        self.emb_heuristics = nn.ModuleList(emb_heuristics)
        ## for ppa
        if args.dataset == 'ogbl-ppa':
            self.id_encoder = nn.Embedding(100, args.dim_encoding)
        ## SMGT layer
        GTMlayers = []
        dim_hidden = args.dim_in if args.dim_hidden is None else args.dim_hidden
        if args.n_layers > 0:
            for i in range(args.n_layers):
                dim_in = args.dim_in if i ==0 else dim_hidden
                GTMlayers.append(GTM(dim_in, dim_hidden, n_heads=args.n_heads, bias=args.bias, residual=args.residual, reduce=args.reduce,
                                     mask_atten=args.mask_atten, mask_combine=args.mask_combine, dim_atten=args.dim_atten, negative_slope=args.negative_slope))
        else:
            self.linear_in = nn.Linear(args.dim_in, dim_hidden, bias=args.bias)

        self.GTMlayers = nn.ModuleList(GTMlayers)
        self.row_norm = nn.LayerNorm(dim_hidden)
        ## MLP
        dim_hidden += args.dim_encoding * self.num_heuristics
        self.mlp = nn.ModuleList([nn.Linear(dim_hidden, dim_hidden, bias=args.bias) for _ in range(args.n_layers_mlp)])
        self.row_norm_mlp = nn.LayerNorm(dim_hidden)
        self.final_out = nn.ModuleList([nn.Linear(dim_hidden, 64, bias=args.bias), nn.Linear(64, 1, bias=args.bias)])

        self.apply(init_params)

        self.mask_decay = Parameter(torch.Tensor(1))


    def forward(self, graph, edge_batch):
        ## input config #####################################
        edge_batch = edge_batch.to(self.args.device, dtype=torch.int64)
        feats = graph.x
        if self.args.dataset == 'ogbl-ppa':
            feats = self.id_encoder(feats.squeeze().to(self.args.device))

        x = torch.zeros((self.args.num_nodes,1)).to(self.args.device)
        if self.args.use_feature and x != None:
            x = torch.cat([x, feats.to(self.args.device)], dim=1)
        if self.args.use_node_emb:
            node_ids = torch.arange(0, self.args.num_nodes).long().to(self.args.device)
            x = torch.cat([x, self.emb_node(node_ids)], dim=1)
        if self.args.use_degree:
            dg = torch.from_numpy(graph.degree).squeeze().long().to(self.args.device)
            x = torch.cat([x, self.emb_degree(dg)], dim=1)
        if x.size(1) > 1:
            x =x[:,1:]

        ## mask config #####################################
        mask = graph.mask.to(self.args.device)
        values = mask.storage._value
        if self.args.mask_weight == 'decay':
            self.mask_decay.data = self.mask_decay.clamp(0.1, 1.5)
            values = torch.exp(-1 * values * self.mask_decay)
        elif self.args.mask_weight == 'same':
            values = values.new_zeros(len(values))+1
        elif self.args.mask_weight == 'weight':
            raise ValueError((f'mask_weight must be options: same, decay'))
        values = F.dropout(values, p=self.args.dropout_mask, training=self.training)
        mask.storage._value = values
        mask = SparseTensor_norm(mask)

        ## start learning ##################################
        ## multi-head graph transformer or transform x from dim_in to dim_hidden
        if len(self.GTMlayers)>0:
            for layer in self.GTMlayers[:-1]:
                x = layer(x, mask)
                # x = self.row_norm(x)
                x = F.relu(x)
                x = F.dropout(x, p=self.dropout, training=self.training)
            x = self.GTMlayers[-1](x, mask)
            #x = F.relu(x)
        else:
            x = self.linear_in(x)
            x = F.relu(x)
        ## use distance and common neighbors
        x = x[edge_batch[:, 0]] * x[edge_batch[:, 1]]
        for i in range(self.num_heuristics):
            x = torch.cat([x, self.emb_heuristics[i](edge_batch[:, i+2])], dim=1)

        ## link prediction
        for linear in self.mlp:
            x = linear(x)
            if self.args.layer_norm_use:
                x = self.row_norm_mlp(x)
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.final_out[0](x)
        x = self.final_out[1](x)
        x = torch.sigmoid(x)
        return x


class GTM_attention(nn.Module):
    def __init__(self, dim_in, dim_out, mask_atten, mask_combine, dim_atten: int=2, negative_slope: float=0.2):
        super().__init__()
        self.mask_atten = mask_atten
        self.mask_combine = mask_combine
        self.negative_slope = negative_slope
        self.square_d = torch.rsqrt(torch.tensor(dim_atten))

        self.linear_row = nn.Linear(dim_in, dim_atten, bias = False)
        self.linear_col = nn.Linear(dim_in, dim_atten, bias=False)
        if self.mask_atten == 'Concat':
            self.linear_concat = nn.Linear(dim_atten*2, 1, bias=False)

        self.linear_x = nn.Linear(dim_in, dim_out)

    def forward(self, x, mask):
        if isinstance(mask, torch_sparse.SparseTensor):
            if self.mask_atten in ['Concat', 'Cosine', 'Multiply']:
                a_row, a_col = self.linear_row(x), self.linear_col(x)
                row, col = mask.storage._row, mask.storage._col
                if self.mask_atten == 'Concat':
                    # Concat attention mechnism, see more in GAT
                    atten = torch.cat((a_row[row], a_col[col]), dim=1)
                    atten = self.linear_concat(atten)
                elif self.mask_atten == 'Cosine':
                    # cosine similarity-based attention mechnism like AGNN
                    atten = F.cosine_similarity(a_row[row], a_col[col], dim=1)
                elif self.mask_atten == 'Multiply':
                    # self-attention mechnism like Transformer
                    atten = a_row[row] * a_col[col]
                    atten = atten.sum(dim=1) * self.square_d
                # softmax attention
                atten = F.leaky_relu(atten, self.negative_slope)
                atten = atten.squeeze()
                atten = atten - atten.max()
                atten = atten.exp()
                atten = mask.set_value(atten, layout='coo')
                atten = SparseTensor_norm(atten)
                # combine attention with mask weight.
                if self.mask_combine == 'plus':
                    atten = (mask.storage._value + atten.storage._value)/2
                    atten = mask.set_value(atten, layout='coo')
                elif self.mask_combine == 'multiply':
                    atten = (mask.storage._value * atten.storage._value)
                    atten = mask.set_value(atten, layout='coo')
                    atten = SparseTensor_norm(atten)
                elif self.mask_combine != 'only_atten':
                    raise ValueError((f'mask_combine must be options: no_use, plus, multiply'))
            elif self.mask_atten == 'no_atten':
                atten = mask
            else:
                raise ValueError((f'mask_atten must be options: Concat, Cosine, Multiply'))

            out = torch_sparse.matmul(atten, x, reduce='sum')
            out = self.linear_x(out)

        return out


class GTM(nn.Module):
    def __init__(self, dim_in: int, dim_out: int, n_heads: int = 1, bias: bool = True,
                 residual: bool = True, reduce: str = 'add', mask_atten: str = 'Multiply', mask_combine: str = 'no_use',
                 dim_atten: int=2, negative_slope: float=0.2):
        super().__init__()

        if residual:
            self.linear_residual = nn.Linear(dim_in, dim_out, bias=bias)
        self.multihead_layer = nn.ModuleList([GTM_attention(dim_in, dim_out, mask_atten, mask_combine, dim_atten, negative_slope) for _ in range(n_heads)])

        self.residual = residual
        self.reduce = reduce
        self.n_heads = n_heads + 1 if residual else n_heads

        if reduce == 'concat':
            self.linear = nn.Linear(dim_out * self.n_heads, dim_out, bias=bias)
        elif reduce == 'add':
            self.linear = nn.Linear(dim_out, dim_out, bias=bias)
        else:
            raise ValueError('args.reduce is set error. Options: concat or add')

    def forward(self, x, mask):
        y = [self.linear_residual(x)] if self.residual else []
        for layer in self.multihead_layer:
            y.append(layer(x, mask))
        if len(y)>1:
            if self.reduce == 'concat':
                y = torch.cat(y, dim=-1)
                y = self.linear(y)
            elif self.reduce == 'add':
                out = y[0]
                for i in range(1,len(y)):
                    out += y[i]
                y = self.linear(out)

        return y


