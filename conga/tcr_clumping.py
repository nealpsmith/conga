# import scanpy as sc
# import random
import pandas as pd
from os.path import exists
from pathlib import Path
from collections import OrderedDict#, Counter
# from sklearn.metrics import pairwise_distances
# from sklearn.utils import sparsefuncs
# from sklearn.decomposition import KernelPCA
import numpy as np
# import scipy
# from scipy.cluster import hierarchy
# from scipy.spatial.distance import squareform, cdist
# from scipy.sparse import issparse#, csr_matrix
from scipy.stats import poisson
# from anndata import AnnData
import sys
import os
from sys import exit
from . import util
from . import preprocess
from .tcrdist import tcr_sampler
import random


def estimate_background_tcrdist_distributions(
        organism,
        tcrs,
        max_dist,
        num_random_samples = 50000,
        pseudocount = 0.25,
        tmpfile_prefix = None,
        background_alpha_chains = None, # default is to get these by shuffling tcrs_for_background_generation
        background_beta_chains = None, #  -- ditto --
        tcrs_for_background_generation = None, # default is to use 'tcrs'
):
    if not util.tcrdist_cpp_available():
        print('conga.tcr_clumping.estimate_background_tcrdist_distributions:: need to compile the C++ tcrdist executables')
        exit(1)

    if tmpfile_prefix is None:
        tmpfile_prefix = Path('./tmp_nbrs{}'.format(random.randrange(1,10000)))
    else:
        tmpfile_prefix = Path(tmpfile_prefix)


    if tcrs_for_background_generation is None:
        # only used when background_alpha_chains and/or background_beta_chains is None
        #tcrs_for_background_generation = tcrs
        # since 10x doesn't always provide allele information, we need to try out alternate
        # alleles to get the best parses...
        tcrs_for_background_generation = tcr_sampler.find_alternate_alleles_for_tcrs(
            organism, tcrs, verbose=False)

    max_dist = int(0.1+max_dist) ## need an integer

    if background_alpha_chains is None or background_beta_chains is None:
        # parse the V(D)J junction regions of the tcrs to define split-points for shuffling
        junctions_df = tcr_sampler.parse_tcr_junctions(organism, tcrs_for_background_generation)

        # resample shuffled single-chain tcrs
        if background_alpha_chains is None:
            background_alpha_chains = tcr_sampler.resample_shuffled_tcr_chains(
                organism, num_random_samples, 'A', junctions_df)
        if background_beta_chains is None:
            background_beta_chains  = tcr_sampler.resample_shuffled_tcr_chains(
                organism, num_random_samples, 'B', junctions_df)

    # save all tcrs to files
    achains_file = str(tmpfile_prefix) + '_bg_achains.tsv'
    bchains_file = str(tmpfile_prefix) + '_bg_bchains.tsv'
    tcrs_file = str(tmpfile_prefix) + '_tcrs.tsv'

    pd.DataFrame({'va'   :[x[0] for x in background_alpha_chains],
                  'cdr3a':[x[2] for x in background_alpha_chains]}).to_csv(achains_file, sep='\t', index=False)

    pd.DataFrame({'vb'   :[x[0] for x in background_beta_chains ],
                  'cdr3b':[x[2] for x in background_beta_chains ]}).to_csv(bchains_file, sep='\t', index=False)

    pd.DataFrame({'va':[x[0][0] for x in tcrs], 'cdr3a':[x[0][2] for x in tcrs],
                  'vb':[x[1][0] for x in tcrs], 'cdr3b':[x[1][2] for x in tcrs]})\
      .to_csv(tcrs_file, sep='\t', index=False)


    # compute distributions vs background chains
    if os.name == 'posix':
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'calc_distributions')
    else:
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'calc_distributions.exe')

    outfile = str(tmpfile_prefix) + '_dists.tsv'

    db_filename = Path.joinpath( Path(util.path_to_tcrdist_cpp_db) , 'tcrdist_info_{}.txt'.format( organism))

    cmd = '{} -f {} -m {} -d {} -a {} -b {} -o {}'\
    .format(exe, tcrs_file, max_dist, db_filename, achains_file, bchains_file, outfile)

    util.run_command(cmd, verbose=True)

    if not exists(outfile):
        print('tcr_clumping:: calc_distributions failed: missing', outfile)
        exit(1)

    counts = np.loadtxt(outfile, dtype=int)
    counts = np.cumsum(counts, axis=1)
    assert counts.shape == (len(tcrs), max_dist+1)
    n_bg_pairs = len(background_alpha_chains) * len(background_beta_chains)
    tcrdist_freqs = np.maximum(pseudocount, counts.astype(float))/n_bg_pairs

    for filename in [achains_file, bchains_file, tcrs_file, outfile]:
        os.remove(filename)

    return tcrdist_freqs


def assess_tcr_clumping(
        adata,
        outfile_prefix,
        radii = [24, 48, 72, 96],
        num_random_samples = 50000, # higher numbers are slower but allow more significant pvalues for extreme clumping
        pvalue_threshold = 1.0,
        verbose=True,
        also_find_clumps_within_gex_clusters=False,
):
    ''' Returns a pandas dataframe with the following columns:
    - clone_index
    - nbr_radius
    - pvalue_adj
    - num_nbrs
    - expected_num_nbrs
    - raw_count
    - va, ja, cdr3a, vb, jb, cdr3b (ie, the 6 tcr cols for clone_index clone)
    - clumping_group: clonotypes within each other's significant nbr_radii are linked
    - clump_type: string, either 'global' or 'intra_gex_cluster' (latter only if also_find_clumps_within_gex_clusters=T)

    '''
    if not util.tcrdist_cpp_available():
        print('conga.tcr_clumping.assess_tcr_clumping:: need to compile the C++ tcrdist executables')
        exit(1)

    if also_find_clumps_within_gex_clusters:
        clusters_gex = np.array(adata.obs['clusters_gex'])

    organism = adata.uns['organism']
    num_clones = adata.shape[0]

    radii = [int(x+0.1) for x in radii] #ensure integers

    outprefix = outfile_prefix + '_tcr_clumping'

    tcrs = preprocess.retrieve_tcrs_from_adata(adata)

    bg_freqs = estimate_background_tcrdist_distributions(
        adata.uns['organism'], tcrs, max(radii), num_random_samples=num_random_samples, tmpfile_prefix=outprefix)

    tcrs_file = outprefix +'_tcrs.tsv'
    adata.obs['va cdr3a vb cdr3b'.split()].to_csv(tcrs_file, sep='\t', index=False)


    # find neighbors in fg tcrs up to max(radii) #######################################

    if os.name == 'posix':
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'find_neighbors')
    else:
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'find_neighbors.exe')

    agroups, bgroups = preprocess.setup_tcr_groups(adata)
    agroups_filename = outprefix+'_agroups.txt'
    bgroups_filename = outprefix+'_bgroups.txt'
    np.savetxt(agroups_filename, agroups, fmt='%d')
    np.savetxt(bgroups_filename, bgroups, fmt='%d')

    db_filename = Path.joinpath( Path(util.path_to_tcrdist_cpp_db), f'tcrdist_info_{organism}.txt')

    tcrdist_threshold = max(radii)

    cmd = '{} -f {} -t {} -d {} -o {} -a {} -b {}'\
    .format(exe, tcrs_file, tcrdist_threshold, db_filename, outprefix, agroups_filename, bgroups_filename)

    util.run_command(cmd, verbose=True)

    nbr_indices_filename = outprefix + '_nbr{}_indices.txt'.format( tcrdist_threshold)
    nbr_distances_filename = outprefix + '_nbr{}_distances.txt'.format( tcrdist_threshold)

    if not exists(nbr_indices_filename) or not exists(nbr_distances_filename):
        print('find_neighbors failed:', exists(nbr_indices_filename), exists(nbr_distances_filename))
        exit(1)

    all_nbrs = []
    all_distances = []
    for line1, line2 in zip(open(nbr_indices_filename,'r'), open(nbr_distances_filename,'r')):
        l1 = line1.split()
        l2 = line2.split()
        assert len(l1) == len(l2)
        #ii = len(all_nbrs)
        all_nbrs.append([int(x) for x in l1])
        all_distances.append([int(x) for x in l2])
    assert len(all_nbrs) == num_clones

    clone_sizes = adata.obs['clone_sizes']

    # use poisson to find nbrhoods with more tcrs than expected; have to handle agroups/bgroups
    dfl = []

    is_clumped = np.full((num_clones,), False)

    n_bg_pairs = num_random_samples * num_random_samples

    for ii in range(num_clones):
        ii_freqs = bg_freqs[ii]
        ii_dists = all_distances[ii]
        for radius in radii:
            num_nbrs = np.sum(x<=radius for x in ii_dists)
            max_nbrs = np.sum( (agroups!=agroups[ii]) & (bgroups!=bgroups[ii]))
            if num_nbrs:
                # adjust for number of tests
                mu = max_nbrs * ii_freqs[radius]
                pval = len(radii) * num_clones * poisson.sf( num_nbrs-1, mu )
                if pval< pvalue_threshold:
                    is_clumped[ii] = True
                    raw_count = ii_freqs[radius]*n_bg_pairs # if count was 0, will be pseudocount
                    if verbose:
                        print('tcr_nbrs_global: {:2d} {:9.6f} radius: {:2d} pval: {:9.1e} {:9.1f} tcr: {:3d} {} {}'\
                              .format( num_nbrs, mu, radius, pval, raw_count, clone_sizes[ii],
                                       ' '.join(tcrs[ii][0][:3]), ' '.join(tcrs[ii][1][:3])))
                    dfl.append( OrderedDict(clump_type='global',
                                            clone_index=ii,
                                            nbr_radius=radius,
                                            pvalue_adj=pval,
                                            num_nbrs=num_nbrs,
                                            expected_num_nbrs=mu,
                                            raw_count=raw_count,
                                            va   =tcrs[ii][0][0],
                                            ja   =tcrs[ii][0][1],
                                            cdr3a=tcrs[ii][0][2],
                                            vb   =tcrs[ii][1][0],
                                            jb   =tcrs[ii][1][1],
                                            cdr3b=tcrs[ii][1][2],
                    ))
                if also_find_clumps_within_gex_clusters:
                    ii_nbrs = all_nbrs[ii]
                    ii_cluster = clusters_gex[ii]
                    ii_cluster_mask = (clusters_gex==ii_cluster)
                    num_nbrs = np.sum( (x<=radius and clusters_gex[y]==ii_cluster) for x,y in zip(ii_dists, ii_nbrs))
                    if num_nbrs:
                        max_nbrs = np.sum( (agroups!=agroups[ii]) & (bgroups!=bgroups[ii]) & ii_cluster_mask)
                        mu = max_nbrs * ii_freqs[radius]
                        pval = len(radii) * num_clones * poisson.sf( num_nbrs-1, mu )
                        if pval< pvalue_threshold:
                            is_clumped[ii] = True
                            raw_count = ii_freqs[radius]*n_bg_pairs # if count was 0, will be pseudocount
                            if verbose:
                                print('tcr_nbrs_intra: {:2d} {:9.6f} radius: {:2d} pval: {:9.1e} {:9.1f} tcr: {:3d} {} {}'\
                                      .format( num_nbrs, mu, radius, pval, raw_count, clone_sizes[ii],
                                               ' '.join(tcrs[ii][0][:3]), ' '.join(tcrs[ii][1][:3])))
                            dfl.append( OrderedDict(clump_type='intra_gex_cluster',
                                                    clone_index=ii,
                                                    nbr_radius=radius,
                                                    pvalue_adj=pval,
                                                    num_nbrs=num_nbrs,
                                                    expected_num_nbrs=mu,
                                                    raw_count=raw_count,
                                                    va   =tcrs[ii][0][0],
                                                    ja   =tcrs[ii][0][1],
                                                    cdr3a=tcrs[ii][0][2],
                                                    vb   =tcrs[ii][1][0],
                                                    jb   =tcrs[ii][1][1],
                                                    cdr3b=tcrs[ii][1][2],
                            ))
    results_df = pd.DataFrame(dfl)
    if results_df.shape[0] == 0:
        return results_df

    # identify groups of related hits?
    all_clumped_nbrs = {}
    for l in results_df.itertuples():
        ii = l.clone_index
        radius = l.nbr_radius
        clumped_nbrs = set(x for x,y in zip(all_nbrs[ii], all_distances[ii]) if y<= radius and is_clumped[x])
        clumped_nbrs.add(ii)
        if ii in all_clumped_nbrs:
            all_clumped_nbrs[ii] = all_clumped_nbrs[ii] | clumped_nbrs
        else:
            all_clumped_nbrs[ii] = clumped_nbrs


    clumped_inds = sorted(all_clumped_nbrs.keys())
    assert len(clumped_inds) == np.sum(is_clumped)

    # make nbrs symmetric
    for ii in clumped_inds:
        for nbr in all_clumped_nbrs[ii]:
            assert nbr in all_clumped_nbrs
            all_clumped_nbrs[nbr].add(ii)

    all_smallest_nbr = {}
    for ii in clumped_inds:
        all_smallest_nbr[ii] = min(all_clumped_nbrs[ii])

    while True:
        updated = False
        for ii in clumped_inds:
            nbr = all_smallest_nbr[ii]
            new_nbr = min(nbr, np.min([all_smallest_nbr[x] for x in all_clumped_nbrs[ii]]))
            if nbr != new_nbr:
                all_smallest_nbr[ii] = new_nbr
                updated = True
        if not updated:
            break
    # define clusters, choose cluster centers
    clusters = np.array([0]*num_clones) # 0 if not clumped

    cluster_number=0
    for ii in clumped_inds:
        nbr = all_smallest_nbr[ii]
        if ii==nbr:
            cluster_number += 1
            members = [ x for x,y in all_smallest_nbr.items() if y==nbr]
            clusters[members] = cluster_number

    for ii, nbrs in all_clumped_nbrs.items():
        for nbr in nbrs:
            assert clusters[ii] == clusters[nbr] # confirm single-linkage clusters

    assert not np.any(clusters[is_clumped]==0)
    assert np.all(clusters[~is_clumped]==0)

    results_df['clumping_group'] = [ clusters[x.clone_index] for x in results_df.itertuples()]

    return results_df


def tcrs_from_dataframe_helper(df, add_j_and_nucseq=False):
    if add_j_and_nucseq:
        return [ ( (x.va, x.ja, x.cdr3a, x.cdr3a_nucseq),
                   (x.vb, x.jb, x.cdr3b, x.cdr3b_nucseq) ) for x in df.itertuples() ]
    else:
        return [ ( (x.va, None, x.cdr3a), (x.vb, None, x.cdr3b) ) for x in df.itertuples() ]

def find_significant_tcrdist_matches(
        query_tcrs_df,
        db_tcrs_df,
        organism,
        tmpfile_prefix = '',
        adjusted_pvalue_threshold = 1.0, # adjusted for size of query_tcrs_df AND db_tcrs_df
        background_tcrs_df = None, # default is to use query_tcrs_df
        num_random_samples_for_bg_freqs = 50000,
        nocleanup=False,
        fixup_allele_assignments_in_background_tcrs_df=True,
):
    ''' Computes paired tcrdist distances between query_tcrs_df and db_tcrs_df and converts
    to p-values, adjusted for both the number of query and db tcrs
    (ie, pvalue_adj = num_query_tcrs * num_db_tcrs * pvalue_raw)

    Anything ending in _df is a pandas DataFrame

    Required columns in query_tcrs_df and db_tcrs_df: va, cdr3a, vb, cdr3b

    Required columns in background_tcrs_df: va ja cdr3a cdr3a_nucseq vb jb cdr3b cdr3b_nucseq

    returns results pd.DataFrame with columns:

    * tcrdist
    * pvalue_adj
    * va, {ja}, cdr3a, vb, {jb}, cdr3b {}:if present in query_tcrs_df
    * PLUS all columns in db_tcrs_df prepended with 'db_' string

    NOTE: reported pvalues are adjusted for sizes of both adata and db_tcrs_tsvfile

    '''

    if background_tcrs_df is None:
        background_tcrs_df = query_tcrs_df

    for tag, df in [ ['query', query_tcrs_df],
                     ['db', db_tcrs_df],
                     ['background', background_tcrs_df]]:
        for ab in 'ab':
            required_cols = f'cdr3{ab} v{ab}'.split()
            if tag == 'background':
                required_cols += f'cdr3{ab}_nucseq j{ab}'.split()
            for col in required_cols:
                if col not in df.columns:
                    print(f'ERROR find_significant_tcrdist_matches:: {tag} df is missing {col} column')
                    return

    query_tcrs = tcrs_from_dataframe_helper(query_tcrs_df)
    db_tcrs = tcrs_from_dataframe_helper(db_tcrs_df)
    background_tcrs = tcrs_from_dataframe_helper(background_tcrs_df, add_j_and_nucseq=True)

    pvalue_adjustment = len(query_tcrs) * len(db_tcrs) # multiply by this to account for multiple tests
    print('pvalue_adjustment:', pvalue_adjustment, len(query_tcrs), len(db_tcrs))

    if fixup_allele_assignments_in_background_tcrs_df:
        background_tcrs = tcr_sampler.find_alternate_alleles_for_tcrs(
            organism, background_tcrs, verbose=False)


    max_dist = 200

    bg_freqs = estimate_background_tcrdist_distributions(
        organism, query_tcrs, max_dist,
        num_random_samples= num_random_samples_for_bg_freqs,
        tcrs_for_background_generation= background_tcrs)
    assert bg_freqs.shape == (len(query_tcrs), max_dist+1)

    adjusted_bg_freqs = pvalue_adjustment * bg_freqs

    could_match = np.any( adjusted_bg_freqs<= adjusted_pvalue_threshold, axis=0)
    assert could_match.shape == (max_dist+1,)

    max_dist_for_matching = 0 # must be some numpy way of doing this
    while could_match[max_dist_for_matching] and max_dist_for_matching<max_dist:
        max_dist_for_matching += 1

    print(f'find_significant_tcrdist_matches:: max_dist: {max_dist} max_dist_for_matching: {max_dist_for_matching}')

    # now run C++ matching code
    query_tcrs_file = tmpfile_prefix+'temp_query_tcrs.tsv'
    db_tcrs_file = tmpfile_prefix+'temp_db_tcrs.tsv'
    query_tcrs_df['va cdr3a vb cdr3b'.split()].to_csv(query_tcrs_file, sep='\t', index=False)
    db_tcrs_df['va cdr3a vb cdr3b'.split()].to_csv(db_tcrs_file, sep='\t', index=False)

    if os.name == 'posix':
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'find_paired_matches')
    else:
        exe = Path.joinpath( Path(util.path_to_tcrdist_cpp_bin) , 'find_paired_matches.exe')

    if not exists(exe):
        print('ERROR: find_paired_matches:: tcrdist_cpp executable {exe} is missing')
        print('ERROR: see instructions in github repository README for compiling')
        return

    db_filename = Path.joinpath( Path(util.path_to_tcrdist_cpp_db), f'tcrdist_info_{organism}.txt')

    outfilename = tmpfile_prefix+'temp_tcr_matching.tsv'

    cmd = '{} -i {} -j {} -t {} -d {} -o {}'\
          .format(exe, query_tcrs_file, db_tcrs_file, max_dist_for_matching, db_filename, outfilename)

    util.run_command(cmd, verbose=True)

    df = pd.read_csv(outfilename, sep='\t')

    dfl = []
    for l in df.itertuples():
        i = l.index1
        j = l.index2
        pvalue_adj = pvalue_adjustment * bg_freqs[i][l.tcrdist]
        if pvalue_adj > adjusted_pvalue_threshold:
            continue
        query_row = query_tcrs_df.iloc[i]
        db_row = db_tcrs_df.iloc[j]
        assert query_row.cdr3b == l.cdr3b1 # sanity check
        assert db_row.cdr3b == l.cdr3b2 # ditto
        D = OrderedDict(tcrdist= l.tcrdist,
                        pvalue_adj=pvalue_adj,
                        query_index= i,
                        db_index= j,
                        va= query_row.va,
                        ja= query_row.ja,
                        cdr3a= query_row.cdr3a,
                        vb= query_row.vb,
                        jb= query_row.jb,
                        cdr3b= query_row.cdr3b)
        if 'ja' in query_row:
            D['ja'] = query_row.ja
        if 'jb' in query_row:
            D['jb'] = query_row.jb
        for tag in db_row.index:
            D['db_'+tag] = db_row[tag]
        dfl.append(D)

    if not nocleanup:
        os.remove(outfilename)
        os.remove(query_tcrs_file)
        os.remove(db_tcrs_file)

    results_df = pd.DataFrame(dfl)
    if dfl:
        results_df.sort_values('pvalue_adj', inplace=True)

    return results_df


def match_adata_tcrs_to_db_tcrs(
        adata,
        db_tcrs_tsvfile=None,
        tmpfile_prefix='',
        adjusted_pvalue_threshold = 1.0, # adjusted for size of adata AND db_tcrs_tsvfile
        tcrs_for_background_generation = None, # default is to use tcrs from adata
        num_random_samples_for_bg_freqs = 50000,
        nocleanup=False,
        fixup_allele_assignments_in_background_tcrs_df=True,
):
    ''' Find significant tcrdist matches tcrs in adata and tcrs in db_tcrs_tsvfile
    by calling find_significant_tcrdist_matches function above (see that docstring too)

    returns results pd.DataFrame with columns tcrdist, pvalue_adj, va, ja, cdr3a, vb, jb, cdr3b,
    PLUS all columns in db_tcrs_tsvfile prepended with 'db_' string

    db_tcrs_tsvfile has at a minimum the columns: va (or va_gene) cdr3a vb (or vb_gene) cdr3b

    NOTE: reported pvalues are adjusted for sizes of both adata and db_tcrs_tsvfile

    '''

    if db_tcrs_tsvfile is None:
        if adata.uns['organism'] != 'human':
            print('ERROR: match_adata_tcrs_to_db_tcrs db_tcrs_tsvfile is None')
            print('but we only have built-in database for organism=human')
            return pd.DataFrame() ##### NOTE EARLY RETURN HERE ################

        print('tcr_clumping.match_adata_tcrs_to_db_tcrs: Matching to default literature TCR database; for more info see conga/data/new_paired_tcr_db_for_matching_nr_README.txt')
        db_tcrs_tsvfile = Path.joinpath(
            util.path_to_data, 'new_paired_tcr_db_for_matching_nr.tsv')

    print('Matching to paired tcrs in', db_tcrs_tsvfile)

    query_tcrs_df = adata.obs['va ja cdr3a vb jb cdr3b'.split()].copy()
    db_tcrs_df = pd.read_csv(db_tcrs_tsvfile, sep='\t')

    # possibly swap legacy column names
    if 'va' not in db_tcrs_df.columns and 'va_gene' in db_tcrs_df.columns:
        db_tcrs_df['va'] = db_tcrs_df['va_gene']
    if 'vb' not in db_tcrs_df.columns and 'vb_gene' in db_tcrs_df.columns:
        db_tcrs_df['vb'] = db_tcrs_df['vb_gene']

    if tcrs_for_background_generation is None:
        background_tcrs_df = adata.obs['va ja cdr3a cdr3a_nucseq vb jb cdr3b cdr3b_nucseq'.split()]\
                                  .copy()
    else:
        background_tcrs_df = pd.DataFrame(
            [ dict(va=x[0], ja=x[1], cdr3a=x[2], cdr3a_nucseq=x[3],
                   vb=y[0], jb=y[1], cdr3b=y[2], cdr3b_nucseq=y[3])
              for x,y in tcrs_for_background_generation ])

    results = find_significant_tcrdist_matches(
        query_tcrs_df,
        db_tcrs_df,
        adata.uns['organism'],
        tmpfile_prefix=tmpfile_prefix,
        adjusted_pvalue_threshold=adjusted_pvalue_threshold,
        background_tcrs_df=background_tcrs_df,
        num_random_samples_for_bg_freqs=num_random_samples_for_bg_freqs,
        fixup_allele_assignments_in_background_tcrs_df=fixup_allele_assignments_in_background_tcrs_df,
        nocleanup=nocleanup,
        )


    results.rename(columns={'query_index':'clone_index'}, inplace=True)
    barcodes = list(adata.obs.index)
    results['barcode'] = [barcodes[x.clone_index] for x in results.itertuples()]

    return results

