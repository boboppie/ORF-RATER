#! /usr/bin/env python

import argparse
import os
import pandas as pd
import numpy as np
from sklearn.grid_search import GridSearchCV
from sklearn.ensemble import RandomForestClassifier
from monotone import monotonic_regressor
import sys
from time import strftime

parser = argparse.ArgumentParser(description='Combine one or more output files from regress_orfs.py into a final translation rating for each ORF. '
                                             'Features will be loaded and calculated from the regression output, and scores will be calculated using '
                                             'a random forest, followed by a monotonization procedure to remove some overfitting artifacts.')
parser.add_argument('regressfile', nargs='+',
                    help='Subdirectory/subdirectories or filename(s) containing regression output from regress_orfs.py, for use in forming a final '
                         'rating. If directory(ies) are provided, they should contain a file named regression.h5. Datasets treated with translation '
                         'inititation inhibitors (e.g. HARR, LTM) for which the --startonly toggle was set in regress_orfs.py will only be used for '
                         'initiation codon results; other datasets will be used for both initiation and termination codons.')
parser.add_argument('--cdsstore', default='cds.h5',
                    help='Path to pandas HDF store containing CDS information; generated by find_annotations.py (Default: cds.h5)')
parser.add_argument('--names', nargs='+', help='Names to use for datasets included in REGRESSFILEs. Should meaningfully indicate the important '
                                               'features of each. (Default: inferred from REGRESSFILEs)')
parser.add_argument('--numtrees', type=int, default=2048, help='Number of trees to use in the random forest (Default: 2048)')
parser.add_argument('--minperleaf', type=int, nargs='+', default=[8, 16, 32, 64, 128],
                    help='Minimum samples per leaf to use in the random forest. Final value will be selected based on cross validation. (Default: '
                         '8 16 32 64 128)')
parser.add_argument('--minforestscore', type=float, default=0.3, help='Minimum forest score to require for monotonization (Default: 0.3)')
parser.add_argument('--cvfold', type=int, default=6, help='Number of folds for random forest cross-validation (Default: 6)')
parser.add_argument('--goldallcodons', action='store_true',
                    help='Random forest training set is normally restricted to ATG-initiated ORFs. If this flag is toggled, training will be '
                         'performed on all ORFs, which may unfairly penalize non-ATG-initiated ORFs.')
parser.add_argument('--goldminlen', type=int, default=100, help='Minimum length (in codons) for ORFs included in the training set (Default: 100)')
parser.add_argument('--ratingsfile', default='orfratings.h5',
                    help='Filename to which to output the final rating for each ORF. Formatted as pandas HDF (table name is "orfratings"). Columns '
                         'include basic information, raw score from random forest, and final monotonized orf rating. For ORFs appearing on multiple '
                         'transcripts, only one transcript will be selected for the table. (Default: orfratings.h5)')
parser.add_argument('-v', '--verbose', action='store_true', help='Output a log of progress and timing (to stdout)')
parser.add_argument('-p', '--numproc', type=int, default=1, help='Number of processes to run. Defaults to 1 but more recommended if available.')
parser.add_argument('-f', '--force', action='store_true', help='Force file overwrite')
opts = parser.parse_args()

if not opts.force and os.path.exists(opts.ratingsfile):
    raise IOError('%s exists; use --force to overwrite' % opts.ratingsfile)

regressfiles = []
colnames = []
for regressfile in opts.regressfile:
    if os.path.isfile(regressfile):
        regressfiles.append(regressfile)
        if not opts.names:
            colnames.append(os.path.basename(regressfile).rpartition(os.path.extsep)[0])  # '/path/to/myfile.h5' -> 'myfile'
    elif os.path.isdir(regressfile) and os.path.isfile(os.path.join(regressfile, 'regression.h5')):
        regressfiles.append(os.path.join(regressfile, 'regression.h5'))
        if not opts.names:
            colnames.append(os.path.basename(regressfile.strip(os.path.pathsep)))  # '/path/to/mydir/' -> 'mydir'
    else:
        raise IOError('Regression file/directory %s not found' % regressfile)

if opts.names:
    if len(opts.regressfile) != len(opts.names):
        raise ValueError('Precisely one name must be provided for each REGRESSFILE')
    colnames = opts.names

if opts.verbose:
    sys.stdout.write(' '.join(sys.argv) + '\n')

    def logprint(nextstr):
        sys.stdout.write('[%s] %s\n' % (strftime('%Y-%m-%d %H:%M:%S'), nextstr))
        sys.stdout.flush()

    logprint('Loading regression output')

orf_columns = ['orfname', 'tfam', 'tid', 'tcoord', 'tstop', 'chrom', 'gcoord', 'gstop', 'strand', 'codon', 'AAlen']
allstarts = pd.DataFrame(columns=['tfam', 'chrom', 'gcoord', 'strand'])
allorfs = pd.DataFrame()
allstops = pd.DataFrame(columns=['tfam', 'chrom', 'gstop', 'strand'])
feature_columns = []
stopcols = []
for (regressfile, colname) in zip(regressfiles, colnames):
    with pd.get_store(regressfile, mode='r') as instore:
        if 'stop_strengths' in instore:
            stopcols.append(colname)
            allstarts = allstarts.merge(instore.select('start_strengths', columns=['tfam', 'chrom', 'gcoord', 'strand', 'start_strength', 'W_start'])
                                        .rename(columns={'start_strength': 'str_start_'+colname,
                                                         'W_start': 'W_start_'+colname}), how='outer').fillna(0.)
            allorfs = allorfs.append(instore.select('orf_strengths', columns=orf_columns), ignore_index=True).drop_duplicates('orfname')
            # This line not actually used for regression output beyond just which ORFs actually got a positive score in at least one regression
            # Safer to use concatenation and drop_duplicates rather than outer merges, in case one ORF somehow was assigned to different transcripts
            allstops = allstops.merge(instore.select('stop_strengths', columns=['tfam', 'chrom', 'gcoord', 'strand', 'stop_strength', 'W_stop'])
                                      .rename(columns={'stop_strength': 'str_stop_'+colname,
                                                       'W_stop': 'W_stop_'+colname}), how='outer').fillna(0.)
            feature_columns.extend(['W_start_'+colname, 'W_stop_'+colname, 'str_stop_'+colname])
        else:
            allstarts = allstarts.merge(instore.select('start_strengths', columns=['tfam', 'chrom', 'gcoord', 'strand', 'W_start'])
                                        .rename(columns={'W_start': 'W_start_'+colname}), how='outer').fillna(0.)
            feature_columns.append('W_start_'+colname)

found_cds = pd.read_hdf(opts.cdsstore, 'found_cds', mode='r', columns=['chrom', 'gcoord', 'gstop', 'strand', 'orfname'])
unfound_cds = pd.read_hdf(opts.cdsstore, 'unfound_cds', mode='r', columns=['chrom', 'gcoord', 'gstop', 'strand'])
all_annot_cds = pd.concat((found_cds.drop('orfname', axis=1), unfound_cds))
all_annot_cds['annot'] = True
orfratings = allorfs[allorfs['gcoord'] != allorfs['gstop']] \
    .merge(allstarts, how='left').merge(allstops, how='left').fillna(0.) \
    .merge(all_annot_cds[['chrom', 'gcoord', 'strand', 'annot']].rename(columns={'annot': 'annot_start'}).drop_duplicates(),
           how='left').fillna({'annot_start': False}) \
    .merge(all_annot_cds[['chrom', 'gstop', 'strand', 'annot']].rename(columns={'annot': 'annot_stop'}).drop_duplicates(),
           how='left').fillna({'annot_stop': False})
orfratings['annot_cds'] = orfratings['orfname'].isin(found_cds['orfname'])

stopgrps = orfratings.groupby(['chrom', 'gstop', 'strand'])
for stopcol in stopcols:
    orfratings['stopset_rel_str_start_'+stopcol] = stopgrps['str_start_'+stopcol].transform(lambda x: x/x.max()).fillna(0.)
    feature_columns.append('stopset_rel_str_start_'+stopcol)

if opts.verbose:
    logprint('Training random forest on features:\n\t'+'\n\t'.join(feature_columns))

if opts.goldallcodons:
    gold_set = (orfratings['AAlen'] >= opts.goldminlen)
else:
    gold_set = ((orfratings['codon'] == 'ATG') & (orfratings['AAlen'] >= opts.goldminlen))
gold_class = orfratings.loc[gold_set, ['annot_start', 'annot_stop']].all(1).values.astype(np.int8)*2 - 1  # convert True/False to +1/-1
gold_feat = orfratings.loc[gold_set, feature_columns].values

if opts.verbose:
    logprint('Gold set contains %d annotated ORFs and %d unannotated ORFs' % ((gold_class > 0).sum(), (gold_class < 0).sum()))

currgrid = GridSearchCV(RandomForestClassifier(n_estimators=opts.numtrees), param_grid={'min_samples_leaf': opts.minperleaf},
                        scoring='accuracy', cv=opts.cvfold, n_jobs=opts.numproc)
currgrid.fit(gold_feat, gold_class)

if opts.verbose:
    logprint('Best estimator has estimated %f accuracy with %d minimum samples per leaf' %
             (currgrid.best_score_, currgrid.best_params_['min_samples_leaf']))

if currgrid.best_params_['min_samples_leaf'] == min(opts.minperleaf) and min(opts.minperleaf) > 1:
    sys.stderr.write('WARNING: Optimal minimum samples per leaf is minimum tested; recommended to test lower values\n')
if currgrid.best_params_['min_samples_leaf'] == max(opts.minperleaf):
    sys.stderr.write('WARNING: Optimal minimum samples per leaf is maximum tested; recommended to test greater values\n')

orfratings['forest_score'] = currgrid.best_estimator_.predict_proba(orfratings[feature_columns].values)[:, 1]

to_monotonize = orfratings['forest_score'] > opts.minforestscore

if opts.verbose:
    logprint('Monotonizing %d ORFs' % to_monotonize.sum())

forest_monoreg = monotonic_regressor()
forest_monoreg.fit(orfratings.loc[to_monotonize, feature_columns].values,
                   orfratings.loc[to_monotonize, 'forest_score'].values,
                   njob=opts.numproc)  # parallelization actually accomplishes almost nothing here, but may as well use it since we have it...

orfratings['orfrating'] = np.nan
orfratings.loc[to_monotonize, 'orfrating'] = forest_monoreg.predict_proba(orfratings.loc[to_monotonize, feature_columns].values)

if opts.verbose:
    logprint('Saving results')

orfratings.to_hdf(opts.ratingsfile, 'orfratings', format='t', data_columns=True)

if opts.verbose:
    logprint('Tasks complete')
