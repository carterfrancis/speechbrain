"""
Alignment code

Author
------
Elena Rastorgueva 2020
"""

import torch


def log_matrix_multiply_max(A, b):
    """
    accounts for the fact that the first dimension is batch_size
    Proper docstring to be added
    """
    b = b.unsqueeze(1)
    x, argmax = torch.max(A + b, dim=2)
    return x, argmax


class ViterbiAligner(torch.nn.Module):
    """
    This class calculates Viterbi alignments in the forward method.
    It also records alignments and creates batches of them for use
    in Viterbi training.

    Arguments
    ---------
    output_folder: str
        It is the folder that the alignments will be stored in when
        saved to disk. Not yet implemented.
    neg_inf: float
        The float used to represent a negative infinite log probability.
        Using `-float("Inf")` tends to give numerical instability.
        A number more negative than -1e5 also sometimes gave errors when
        the `genbmm` library was used (currently not in use).
        Default: -1e5


    Example
    -------
    >>> log_posteriors = torch.tensor([[[ -1., -10., -10.],
    ...                                 [-10.,  -1., -10.],
    ...                                 [-10., -10.,  -1.]],
    ...
    ...                                [[ -1., -10., -10.],
    ...                                 [-10.,  -1., -10.],
    ...                                 [-10., -10., -10.]]])
    >>> lens = torch.tensor([1., 0.66])
    >>> phns = torch.tensor([[0, 1, 2],
    ...                      [0, 1, 0]])
    >>> phn_lens = torch.tensor([1., 0.66])
    >>> viterbi_aligner = ViterbiAligner()
    >>> alignments, viterbi_scores = viterbi_aligner(
    ...        log_posteriors, lens, phns, phn_lens
    ... )
    >>> alignments
    [[0, 1, 2], [0, 1]]
    >>> viterbi_scores.shape
    torch.Size([2])
    """

    def __init__(self, output_folder="", neg_inf=-1e5):
        super().__init__()
        self.output_folder = output_folder
        self.neg_inf = neg_inf
        self.align_dict = {}

    def _make_pi_prob(self, phn_lens_abs):
        """
        Creates tensor of initial (log) probabilities (known as 'pi').
        Assigns all probability mass to first phoneme in the sequence.

        Arguments
        ---------
        phn_lens_abs: torch.Tensor (batch)
            The absolute length of each phoneme sequence in the batch.

        Returns
        -------
        pi_prob: torch.Tensor (batch, phn)
        """
        # TODO: can add example to docstring

        batch_size = len(phn_lens_abs)
        U_max = int(phn_lens_abs.max())

        pi_prob = self.neg_inf * torch.ones([batch_size, U_max])
        pi_prob[:, 0] = 0

        return pi_prob

    def _make_trans_prob(self, phn_lens_abs):
        """
        Creates tensor of transition (log) probabilities.
        Allows transitions to the same phoneme (self-loop) or the next
        phoneme in the phn sequence

        Arguments
        ---------
        phn_lens_abs: torch.Tensor (batch)
            The absolute length of each phoneme sequence in the batch.


        Returns
        -------
        trans_prob: torch.Tensor (batch, from, to)

        """
        # Extract useful values for later
        batch_size = len(phn_lens_abs)
        U_max = int(phn_lens_abs.max())
        device = phn_lens_abs.device

        ## trans_prob matrix consists of 2 diagonals:
        ## (1) offset diagonal (next state) &
        ## (2) main diagonal (self-loop)
        # make offset diagonal
        trans_prob_off_diag = torch.eye(U_max - 1)
        zero_side = torch.zeros([U_max - 1, 1])
        zero_bottom = torch.zeros([1, U_max])
        trans_prob_off_diag = torch.cat((zero_side, trans_prob_off_diag), 1)
        trans_prob_off_diag = torch.cat((trans_prob_off_diag, zero_bottom), 0)

        # make main diagonal
        trans_prob_main_diag = torch.eye(U_max)

        # join the diagonals and repeat for whole batch
        trans_prob = trans_prob_off_diag + trans_prob_main_diag
        trans_prob = (
            trans_prob.reshape(1, U_max, U_max)
            .repeat(batch_size, 1, 1)
            .to(device)
        )

        # clear probabilities for too-long sequences
        mask_a = torch.arange(U_max).to(device)[None, :] < phn_lens_abs[:, None]
        mask_a = mask_a.unsqueeze(2)
        mask_a = mask_a.expand(-1, -1, U_max)
        mask_b = mask_a.permute(0, 2, 1)
        trans_prob = trans_prob * (mask_a & mask_b).float()

        ## put -infs in place of zeros:
        trans_prob = torch.where(
            trans_prob == 1, trans_prob, torch.tensor(-float("Inf")).to(device)
        )

        ## normalize
        trans_prob = torch.nn.functional.log_softmax(trans_prob, dim=2)

        ## set nans to v neg numbers
        trans_prob[trans_prob != trans_prob] = self.neg_inf
        ## set -infs to v neg numbers
        trans_prob[trans_prob == -float("Inf")] = self.neg_inf

        return trans_prob

    def _make_emiss_pred_useful(
        self, emission_pred, lens_abs, phn_lens_abs, phns
    ):
        """
        Creates a 'useful' form of the posterior probabilities, rearranged
        into order of phoneme appearance in phns.

        Arguments
        ---------
        emission_pred: torch.Tensor (batch, time, phoneme in vocabulary)
            posterior probabilities from our acoustic model
        lens_abs: torch.Tensor (batch)
            The absolute length of each input to the acoustic model,
            i.e. the number of frames
        phn_lens_abs: torch.Tensor (batch)
            The absolute length of each phoneme sequence in the batch.
        phns: torch.Tensor (batch, phoneme in phn sequence)
            The phonemes that are known/thought to be to be in each utterance

        Returns
        -------
        emiss_pred_useful: torch.Tensor (batch, phoneme in phn sequence, time)
        """
        # Extract useful values for later
        U_max = int(phn_lens_abs.max().item())
        fb_max_length = int(lens_abs.max().item())
        device = emission_pred.device

        # apply mask based on lens_abs
        mask_lens = (
            torch.arange(fb_max_length).to(device)[None, :] < lens_abs[:, None]
        )

        emiss_pred_acc_lens = torch.where(
            mask_lens[:, :, None],
            emission_pred,
            torch.tensor([self.neg_inf]).to(device),
        )

        # manipulate phn tensor, and then 'torch.gather'
        phns = phns.to(device)
        phns_copied = phns.unsqueeze(1).expand(-1, fb_max_length, -1)
        emiss_pred_useful = torch.gather(emiss_pred_acc_lens, 2, phns_copied)

        # apply mask based on phn_lens_abs
        mask_phn_lens = (
            torch.arange(U_max).to(device)[None, :] < phn_lens_abs[:, None]
        )
        emiss_pred_useful = torch.where(
            mask_phn_lens[:, None, :],
            emiss_pred_useful,
            torch.tensor([self.neg_inf]).to(device),
        )

        emiss_pred_useful = emiss_pred_useful.permute(0, 2, 1)

        return emiss_pred_useful

    def _calc_viterbi_alignments(
        self,
        pi_prob,
        trans_prob,
        emiss_pred_useful,
        lens_abs,
        phn_lens_abs,
        phns,
    ):
        """
        Calculates Viterbi alignment using dynamic programming

        Arguments
        ---------
        pi_prob: torch.Tensor (batch, phn)
            Tensor containing initial (log) probabilities

        trans_prob: torch.Tensor (batch, from, to)
            Tensor containing transition (log) probabilities.

        emiss_pred_useful: torch.Tensor (batch, phoneme in phn sequence, time)
            A 'useful' form of the posterior probabilities, rearranged
            into order of phoneme appearance in phns.

        lens_abs: torch.Tensor (batch)
            The absolute length of each input to the acoustic model,
            i.e. the number of frames

        phn_lens_abs: torch.Tensor (batch)
            The absolute length of each phoneme sequence in the batch.

        phns: torch.Tensor (batch, phoneme in phn sequence)
            The phonemes that are known/thought to be to be in each utterance.

        """

        # useful values
        batch_size = len(phn_lens_abs)
        U_max = phn_lens_abs.max()
        fb_max_length = lens_abs.max()
        device = emiss_pred_useful.device

        v_matrix = self.neg_inf * torch.ones(
            [batch_size, U_max, fb_max_length]
        ).to(device)
        backpointers = -99 * torch.ones([batch_size, U_max, fb_max_length])

        # for cropping v_matrix later
        phn_len_mask = torch.arange(U_max)[None, :].to(device) < phn_lens_abs[
            :, None
        ].to(device)

        # initialise
        v_matrix[:, :, 0] = pi_prob + emiss_pred_useful[:, :, 0]

        for t in range(2, fb_max_length + 1):  # note: t here is 1+ indexing
            x, argmax = log_matrix_multiply_max(
                trans_prob.permute(0, 2, 1), v_matrix[:, :, t - 2]
            )
            v_matrix[:, :, t - 1] = x + emiss_pred_useful[:, :, t - 1]

            # crop v_matrix
            v_matrix = torch.where(
                phn_len_mask[:, :, None],
                v_matrix,
                torch.tensor(self.neg_inf).to(device),
            )

            backpointers[:, :, t - 1] = argmax.type(torch.FloatTensor)

        z_stars = []
        z_stars_loc = []

        for utterance_in_batch in range(batch_size):
            len_abs = lens_abs[utterance_in_batch]
            U = phn_lens_abs[utterance_in_batch].long().item()

            z_star_i_loc = [U - 1]
            z_star_i = [phns[utterance_in_batch, z_star_i_loc[0]].item()]
            for time_step in range(len_abs, 1, -1):
                current_best_loc = z_star_i_loc[0]

                earlier_best_loc = (
                    backpointers[
                        utterance_in_batch, current_best_loc, time_step - 1
                    ]
                    .long()
                    .item()
                )
                earlier_z_star = phns[
                    utterance_in_batch, earlier_best_loc
                ].item()

                z_star_i_loc.insert(0, earlier_best_loc)
                z_star_i.insert(0, earlier_z_star)
            z_stars.append(z_star_i)
            z_stars_loc.append(z_star_i_loc)

        #            print("batch alignment statistics:")
        #            print("phn_lens_abs:", phn_lens_abs)
        #            print("lens_abs:", lens_abs)
        #            print("z_stars_loc:", z_stars_loc)
        #            print("z_stars:", z_stars)

        # picking out viterbi_scores
        viterbi_scores = v_matrix[
            torch.arange(batch_size), phn_lens_abs - 1, lens_abs - 1
        ]

        return z_stars, z_stars_loc, viterbi_scores

    def forward(self, emission_pred, lens, phns, phn_lens):
        """
        Prepares relevant (log) probability tensors and calculates Viterbi
        alignments for utterances in the batch.

        Arguments
        ---------
        emission_pred: torch.Tensor (batch, time, phoneme in vocabulary)
            posterior probabilities from our acoustic model
        lens: torch.Tensor (batch)
            The relative duration of each utterance sound file.
        phns: torch.Tensor (batch, phoneme in phn sequence)
            The phonemes that are known/thought to be to be in each utterance
        phn_lens: torch.Tensor (batch)
            The relative length of each phoneme sequence in the batch.
        """

        lens_abs = torch.round(emission_pred.shape[1] * lens).long()
        phn_lens_abs = torch.round(phns.shape[1] * phn_lens).long()
        phns = phns.long()

        pi_prob = self._make_pi_prob(phn_lens_abs)
        trans_prob = self._make_trans_prob(phn_lens_abs)
        emiss_pred_useful = self._make_emiss_pred_useful(
            emission_pred, lens_abs, phn_lens_abs, phns
        )

        alignments, _, viterbi_scores = self._calc_viterbi_alignments(
            pi_prob, trans_prob, emiss_pred_useful, lens_abs, phn_lens_abs, phns
        )

        return alignments, viterbi_scores

    def store_alignments(self, ids, alignments):
        """
        Records alignments in `self.align_dict`.

        Arguments
        ---------
        ids: list of str
            IDs of the files in the batch
        alignments: list of lists of int
            Viterbi alignments for the files in the batch.
            Without padding.

        Example
        -------
        To be added.

        """

        for i, id in enumerate(ids):
            alignment_i = alignments[i]
            self.align_dict[id] = alignment_i

    def _get_flat_start_batch(self, lens_abs, phn_lens_abs, phns):
        """
        Prepares flat start alignments (with zero padding) for every utterance
        in the batch.
        Every phoneme will have equal duration, except for the final phoneme
        potentially. E.g. if 104 frames and 10 phonemes, 9 phonemes will have
        duration of 10 frames, and one phoneme will have duration of 14 frames.

        Arguments
        ---------
        lens_abs: torch.Tensor (batch)
            The absolute length of each input to the acoustic model,
            i.e. the number of frames

        phn_lens_abs: torch.Tensor (batch)
            The absolute length of each phoneme sequence in the batch.

        phns: torch.Tensor (batch, phoneme in phn sequence)
            The phonemes that are known/thought to be to be in each utterance.
        """
        phns = phns.long()

        batch_size = len(lens_abs)
        fb_max_length = torch.max(lens_abs)

        flat_start_batch = torch.zeros(batch_size, fb_max_length).long()
        for i in range(batch_size):
            utter_phns = phns[i]
            utter_phns = utter_phns[: phn_lens_abs[i]]  # crop out zero padding
            repeat_amt = int(lens_abs[i] / len(utter_phns))

            utter_phns = utter_phns.repeat_interleave(repeat_amt)

            # len(utter_phns) now may not equal lens[i]
            # pad out with final phoneme to make lengths equal
            utter_phns = torch.nn.functional.pad(
                utter_phns,
                (0, int(lens_abs[i]) - len(utter_phns)),
                value=utter_phns[-1],
            )

            flat_start_batch[i, : len(utter_phns)] = utter_phns

        return flat_start_batch

    def _get_viterbi_batch(self, ids, lens_abs):
        """
        Retrieves Viterbi alignments stored in `self.align_dict` and
        creates batch of them, with zero padding.

        Arguments
        ---------
        ids: list of str
            IDs of the files in the batch
        lens_abs: torch.Tensor (batch)
            The absolute length of each input to the acoustic model,
            i.e. the number of frames

        """
        batch_size = len(lens_abs)
        fb_max_length = torch.max(lens_abs)

        viterbi_batch = torch.zeros(batch_size, fb_max_length).long()
        for i in range(batch_size):
            viterbi_preds = self.align_dict[ids[i]]
            viterbi_preds = torch.tensor(viterbi_preds)
            viterbi_preds = torch.nn.functional.pad(
                viterbi_preds, (0, fb_max_length - len(viterbi_preds))
            )

            viterbi_batch[i] = viterbi_preds.long()

        return viterbi_batch

    def get_prev_alignments(self, ids, emission_pred, lens, phns, phn_lens):
        """
        Fetches previously recorded Viterbi alignments if they are available.
        If not, fetches flat start alignments.
        Currently, assumes that if a Viterbi alignment is not availble for the
        first utterance in the batch, it will not be available for the rest of
        the utterances.

        Arguments
        ---------
        ids: list of str
            IDs of the files in the batch
        emission_pred: torch.Tensor (batch, time, phoneme in vocabulary)
            posterior probabilities from our acoustic model
        lens: torch.Tensor (batch)
            The relative duration of each utterance sound file.
        phns: torch.Tensor (batch, phoneme in phn sequence)
            The phonemes that are known/thought to be to be in each utterance
        phn_lens: torch.Tensor (batch)
            The relative length of each phoneme sequence in the batch.

        Output
        ------
        torch.Tensor (batch, time)
            Zero-padded alignments

        Example
        -------
        To be added
        """

        lens_abs = torch.round(emission_pred.shape[1] * lens).long()
        phn_lens_abs = torch.round(phns.shape[1] * phn_lens).long()

        if ids[0] in self.align_dict:
            return self._get_viterbi_batch(ids, lens_abs)
        else:
            return self._get_flat_start_batch(lens_abs, phn_lens_abs, phns)
