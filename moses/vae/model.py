import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import glob
import pandas as pd


class VAE(nn.Module):
    def __init__(self, vocab, config):
        super().__init__()
        print("loading VAE")
        self.vocabulary = vocab
        # Special symbols
        for ss in ("bos", "eos", "unk", "pad"):
            setattr(self, ss, getattr(vocab, ss))

        # Word embeddings layer
        # n_vocab, d_emb = len(vocab), vocab.vectors.size(1)
        n_vocab, d_emb = len(vocab), len(vocab)
        self.x_emb = nn.Embedding(n_vocab, d_emb, self.pad)
        # self.x_emb.weight.data.copy_(vocab.vectors)
        if config.freeze_embeddings:
            self.x_emb.weight.requires_grad = False

        # Encoder
        if config.q_cell == "gru":
            self.encoder_rnn = nn.GRU(
                d_emb,
                config.q_d_h,
                num_layers=config.q_n_layers,
                batch_first=True,
                dropout=config.q_dropout if config.q_n_layers > 1 else 0,
                bidirectional=config.q_bidir,
            )
        else:
            raise ValueError("Invalid q_cell type, should be one of the ('gru',)")

        q_d_last = config.q_d_h * (2 if config.q_bidir else 1)
        self.q_mu = nn.Linear(q_d_last, config.d_z)
        self.q_logvar = nn.Linear(q_d_last, config.d_z)

        # Decoder
        if config.d_cell == "gru":
            self.decoder_rnn = nn.GRU(
                d_emb + config.d_z,
                config.d_d_h,
                num_layers=config.d_n_layers,
                batch_first=True,
                dropout=config.d_dropout if config.d_n_layers > 1 else 0,
            )
        else:
            raise ValueError("Invalid d_cell type, should be one of the ('gru',)")

        self.decoder_lat = nn.Linear(config.d_z, config.d_d_h)
        self.decoder_fc = nn.Linear(config.d_d_h, n_vocab)

        # Grouping the model's parameters
        self.encoder = nn.ModuleList([self.encoder_rnn, self.q_mu, self.q_logvar])
        self.decoder = nn.ModuleList(
            [self.decoder_rnn, self.decoder_lat, self.decoder_fc]
        )
        self.vae = nn.ModuleList([self.x_emb, self.encoder, self.decoder])

    @property
    def device(self):
        return next(self.parameters()).device

    def string2tensor(self, string, device="model"):
        ids = self.vocabulary.string2ids(string, add_bos=True, add_eos=True)
        tensor = torch.tensor(
            ids, dtype=torch.long,
            device=self.device if device == 'model' else device
        )

        return tensor

    def tensor2string(self, tensor):
        ids = tensor.tolist()
        string = self.vocabulary.ids2string(ids, rem_bos=True, rem_eos=True)

        return string

    def forward(self, x):
        """Do the VAE forward step

        :param x: list of tensors of longs, input sentence x
        :return: float, kl term component of loss
        :return: float, recon component of loss
        """

        # Encoder: x -> z, kl_loss
        z, kl_loss = self.forward_encoder(x)

        # Decoder: x, z -> recon_loss
        pred = self.forward_decoder(x, z)
        recon_loss = self.compute_loss(x, pred)

        return kl_loss, recon_loss

    def forward_encoder(self, x):
        """Encoder step, emulating z ~ E(x) = q_E(z|x)

        :param x: list of tensors of longs, input sentence x
        :return: (n_batch, d_z) of floats, sample of latent vector z
        :return: float, kl term component of loss
        """

        x = [self.x_emb(i_x) for i_x in x]
        x = nn.utils.rnn.pack_sequence(x)

        _, h = self.encoder_rnn(x, None)

        h = h[-(1 + int(self.encoder_rnn.bidirectional)) :]
        h = torch.cat(h.split(1), dim=-1).squeeze(0)

        mu, logvar = self.q_mu(h), self.q_logvar(h)
        eps = torch.randn_like(mu)
        z = mu + (logvar / 2).exp() * eps

        kl_loss = 0.5 * (logvar.exp() + mu ** 2 - 1 - logvar).sum(1).mean()

        return z, kl_loss

    def forward_decoder(self, x, z):
        """Decoder step, emulating x ~ G(z)

        :param x: list of tensors of longs, input sentence x
        :param z: (n_batch, d_z) of floats, latent vector z
        :return: float, recon component of loss
        """

        lengths = [len(i_x) for i_x in x]

        x = nn.utils.rnn.pad_sequence(x, batch_first=True, padding_value=self.pad)
        x_emb = self.x_emb(x)

        z_0 = z.unsqueeze(1).repeat(1, x_emb.size(1), 1)
        x_input = torch.cat([x_emb, z_0], dim=-1)
        x_input = nn.utils.rnn.pack_padded_sequence(x_input, lengths, batch_first=True)

        h_0 = self.decoder_lat(z)
        h_0 = h_0.unsqueeze(0).repeat(self.decoder_rnn.num_layers, 1, 1)

        output, _ = self.decoder_rnn(x_input, h_0)

        output, _ = nn.utils.rnn.pad_packed_sequence(output, batch_first=True)
        y = self.decoder_fc(output)
        return y

    def compute_loss(x, y):

        recon_loss = F.cross_entropy(
            y[:, :-1].contiguous().view(-1, y.size(-1)),
            x[:, 1:].contiguous().view(-1),
            ignore_index=self.pad,
        )

        return recon_loss

    def reconstruct(self, tqdm_data, save_path):
        all_samples = []
        for i, input_batch in enumerate(tqdm_data):
            input_batch = tuple(data.to(self.device) for data in input_batch)
            with torch.no_grad():
                z, _ = self.forward_encoder(input_batch)
                output = self.forward_decoder(input_batch, z)
                output = F.log_softmax(output, dim=2)  # (B,L,V)

            smiles = []
            for s in range(output.size(0)):  # for each sample in batch
                sample = []
                for c in range(output.size(1)):  # char in sequence
                    v = torch.argmax(output[s][c]).item()
                    if v == self.eos or v == self.pad:
                        break
                    else:
                        sample.append(v)
                pred_sm = self.tensor2string(torch.Tensor(sample))
                smiles.append(pred_sm)

            all_samples.extend(smiles)

        all_samples = pd.DataFrame(all_samples, columns=["SMILES"])
        all_samples.to_csv(save_path, index=False)
        return

    def sample_z_prior(self, n_batch):
        """Sampling z ~ p(z) = N(0, I)

        :param n_batch: number of batches
        :return: (n_batch, d_z) of floats, sample of latent z
        """

        return torch.randn(
            n_batch, self.q_mu.out_features, device=self.x_emb.weight.device
        )

    def sample(self, n_batch, max_len=100, z=None, temp=1.0):
        """Generating n_batch samples in eval mode (`z` could be
        not on same device)

        :param n_batch: number of sentences to generate
        :param max_len: max len of samples
        :param z: (n_batch, d_z) of floats, latent vector z or None
        :param temp: temperature of softmax
        :return: list of tensors of strings, samples sequence x
        """
        with torch.no_grad():
            if z is None:
                z = self.sample_z_prior(n_batch)
            z = z.to(self.device)
            z_0 = z.unsqueeze(1)

            # Initial values
            h = self.decoder_lat(z)
            h = h.unsqueeze(0).repeat(self.decoder_rnn.num_layers, 1, 1)
            w = torch.tensor(self.bos, device=self.device).repeat(n_batch)
            x = torch.tensor([self.pad], device=self.device).repeat(n_batch,
                                                                    max_len)
            x[:, 0] = self.bos

            end_pads = torch.tensor([max_len], device=self.device).repeat(n_batch)
            eos_mask = torch.zeros(n_batch, dtype=torch.bool, device=self.device)

            # Generating cycle
            for i in range(1, max_len):
                x_emb = self.x_emb(w).unsqueeze(1)
                x_input = torch.cat([x_emb, z_0], dim=-1)

                o, h = self.decoder_rnn(x_input, h)
                y = self.decoder_fc(o.squeeze(1))
                y = F.softmax(y / temp, dim=-1)

                w = torch.multinomial(y, 1)[:, 0]
                x[~eos_mask, i] = w[~eos_mask]
                i_eos_mask = ~eos_mask & (w == self.eos)
                end_pads[i_eos_mask] = i + 1
                eos_mask = eos_mask | i_eos_mask

            # Converting `x` to list of tensors
            new_x = []
            for i in range(x.size(0)):
                new_x.append(x[i, :end_pads[i]])


            return [self.tensor2string(i_x) for i_x in new_x]

    def load_lbann_weights(self, weights_dir, epoch_count=-1):
        print("Loading LBANN Weights ")
        if epoch_count < 0:
            epoch_count = "*"

        with torch.no_grad():
            emb_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*-emb_matrix-Weights.txt"
                )[0]
            )
            self.x_emb.weight.data.copy_(torch.from_numpy(np.transpose(emb_weights)))

            # q_logvar_weights = np.loadtxt(glob.glob(weights_dir+"*.epoch."+str(epoch_count)+"*-molvae_module1_encoder_qlogvar_matrix-Weights.txt")[0])

            q_logvar_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*qlogvar_matrix-Weights.txt"
                )[0]
            )
            self.q_logvar.weight.data.copy_(torch.from_numpy(q_logvar_weights))
            #q_logvar_bias = np.loadtxt(glob.glob(weights_dir+ "*.epoch."+ str(epoch_count)+ "*-molvae_module1_encoder_qlogvar_bias-Weights.txt")[0])
            q_logvar_bias = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*qlogvar_bias-Weights.txt"
                )[0]
            )
            self.q_logvar.bias.data.copy_(torch.from_numpy(q_logvar_bias))
            
            """
            q_mu_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*-molvae_module1_encoder_qmu_matrix-Weights.txt"
                )[0]
            )
            """


            q_mu_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*qmu_matrix-Weights.txt"
                )[0]
            )


            self.q_mu.weight.data.copy_(torch.from_numpy(q_mu_weights))
            q_mu_bias = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*-molvae_module1_encoder_qmu_bias-Weights.txt"
                )[0]
            )
            self.q_mu.bias.data.copy_(torch.from_numpy(q_mu_bias))

            decoder_lat_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*-molvae_module1_decoder_lat_matrix-Weights.txt"
                )[0]
            )
            self.decoder_lat.weight.data.copy_(torch.from_numpy(decoder_lat_weights))
            decoder_lat_bias = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*-molvae_module1_decoder_lat_bias-Weights.txt"
                )[0]
            )
            self.decoder_lat.bias.data.copy_(torch.from_numpy(decoder_lat_bias))

            # Load RNN weights/biases
            param_idx = ["_ih_matrix", "_hh_matrix", "_ih_bias", "_hh_bias"]
            for l in range(self.encoder_rnn.num_layers):
                for idx, val in enumerate(param_idx):
                    param_tensor = np.loadtxt(
                        glob.glob(
                            weights_dir
                            + "*.epoch."
                            + str(epoch_count)
                            + "*-molvae_module1_encoder_rnn*"
                            + val
                            + "-Weights.txt"
                        )[0]
                    )
                    self.encoder_rnn.all_weights[l][idx].copy_(
                        torch.from_numpy(param_tensor)
                    )

            for l in range(self.decoder_rnn.num_layers):
                for idx, val in enumerate(param_idx):
                    param_tensor = np.loadtxt(
                        glob.glob(
                            weights_dir
                            + "*.epoch."
                            + str(epoch_count)
                            + "*-molvae_module1_decoder_rnn*"
                            + str(l)
                            + val
                            + "-Weights.txt"
                        )[0]
                    )
                    self.decoder_rnn.all_weights[l][idx].copy_(
                        torch.from_numpy(param_tensor)
                    )

            # Load Linear layer weights/biases
            decoder_fc_weights = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*_decoder_fc_matrix-Weights.txt"
                )[0]
            )
            self.decoder_fc.weight.data.copy_(torch.from_numpy(decoder_fc_weights))
            decoder_fc_bias = np.loadtxt(
                glob.glob(
                    weights_dir
                    + "*.epoch."
                    + str(epoch_count)
                    + "*_decoder_fc_bias-Weights.txt"
                )[0]
            )
            self.decoder_fc.bias.data.copy_(torch.from_numpy(decoder_fc_bias))

            print("DONE loading LBANN weights ")

    def encode_smiles(self, smiles):
        from tqdm import tqdm

        tensor_list = []
        for smile in tqdm(smiles, desc="converting smiles to tensors"):
            tensor_list.append(self.string2tensor(smile).view(1, -1))

        latent_list = []
        for i, input_batch in enumerate(tensor_list):
            input_batch = tuple(data.to(self.device) for data in input_batch)
            with torch.no_grad():
                z, _ = self.forward_encoder(input_batch)
                output = self.forward_decoder(input_batch, z)
                output = F.log_softmax(output, dim=2)  # (B,L,V)
                latent_list.append(output)

        return latent_list, smiles

    def decode_smiles(self, latent_array):
        all_samples = []
        for latent in latent_array:
            latent = torch.from_numpy(latent)
            smiles = []
            for s in range(latent.shape[0]):  # for each sample in batch
                sample = []
                for c in range(latent.shape[1]):  # char in sequence
                    v = torch.argmax(latent[s][c]).item()
                    if v == self.eos or v == self.pad:
                        break
                    else:
                        sample.append(v)
                pred_sm = self.tensor2string(torch.Tensor(sample))
                smiles.append(pred_sm)

            all_samples.extend(smiles)

        all_samples = pd.DataFrame(all_samples, columns=["SMILES"])

        return all_samples, latent
