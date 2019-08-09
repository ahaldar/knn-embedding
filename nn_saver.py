'''
Get the top k nearest neighbors for a set of embeddings and save to a file
'''

import multiprocessing as mp
import tensorflow as tf
import numpy as np
import codecs
import os
from nearest_neighbors import MultiNearestNeighbors
import pyemblib
import io_lib
from hedgepig_logger import log
from drgriffis.common import util

class _SIGNALS:
    HALT = -1
    COMPUTE = 1

def KNearestNeighbors(emb_arrs, node_IDs, top_k, neighbor_file, threads=2,
        batch_size=5, completed_neighbors=None, with_distances=False):
    '''docstring goes here
    '''
    # set up threads
    log.writeln('1 | Thread initialization')
    all_indices = list(range(len(emb_arrs[0])))
    if completed_neighbors:
        filtered_indices = []
        for ix in all_indices:
            if not ix in completed_neighbors:
                filtered_indices.append(ix)
        all_indices = filtered_indices
        log.writeln('  >> Filtered out {0:,} completed indices'.format(len(emb_arrs[0]) - len(filtered_indices)))
        log.writeln('  >> Filtered set size: {0:,}'.format(len(all_indices)))
    index_subsets = util.prepareForParallel(all_indices, threads-1, data_only=True)
    nn_q = mp.Queue()
    nn_writer = mp.Process(
        target=_nn_writer,
        args=(neighbor_file, node_IDs, None, nn_q, with_distances)
    )
    computers = [
        mp.Process(
            target=_threadedNeighbors,
            args=(index_subsets[i], emb_arrs, batch_size, top_k, nn_q, with_distances)
        )
            for i in range(threads - 1)
    ]
    nn_writer.start()
    log.writeln('2 | Neighbor computation')
    util.parallelExecute(computers)
    nn_q.put(_SIGNALS.HALT)
    nn_writer.join()

def KNearestNeighborsFromQueries(emb_arrs, node_IDs, query_emb_arrs,
        query_node_IDs, top_k, neighbor_file, threads=2,
        batch_size=5, completed_neighbors=None, with_distances=False):
    '''docstring goes here
    '''
    # set up threads
    log.writeln('1 | Thread initialization')
    all_indices = list(range(len(query_emb_arrs[0])))
    log.writeln('  ALL INDICES COUNT: %d' % len(all_indices))
    if completed_neighbors:
        filtered_indices = []
        for ix in all_indices:
            if not ix in completed_neighbors:
                filtered_indices.append(ix)
        all_indices = filtered_indices
        log.writeln('  >> Filtered out {0:,} completed indices'.format(len(emb_arrs[0]) - len(filtered_indices)))
        log.writeln('  >> Filtered set size: {0:,}'.format(len(all_indices)))
    index_subsets = util.prepareForParallel(all_indices, threads-1, data_only=True)
    nn_q = mp.Queue()
    nn_writer = mp.Process(
        target=_nn_writer,
        args=(neighbor_file, node_IDs, query_node_IDs, nn_q, with_distances)
    )
    computers = [
        mp.Process(
            target=_threadedCrossSetNeighbors,
            args=(index_subsets[i], query_emb_arrs, emb_arrs, batch_size, top_k, nn_q, with_distances)
        )
            for i in range(threads - 1)
    ]
    nn_writer.start()
    log.writeln('2 | Neighbor computation')
    util.parallelExecute(computers)
    nn_q.put(_SIGNALS.HALT)
    nn_writer.join()

def _nn_writer(neighborf, node_IDs, query_node_IDs, nn_q, with_distances):
    stream = open(neighborf, 'w')
    stream.write('# File format is:\n# <word vocab index>,<NN 1>,<NN 2>,...\n')

    total = len(node_IDs) if query_node_IDs is None else len(query_node_IDs)

    result = nn_q.get()
    log.track(message='  >> Processed {0}/{1:,} samples'.format('{0:,}', total), writeInterval=50)
    while result != _SIGNALS.HALT:
        (ix, neighbors) = result
        if with_distances:
            mapped_neighbors = [
                (node_IDs[nbr], dist)
                    for (nbr, dist) in neighbors
            ]
        else:
            mapped_neighbors = [
                node_IDs[nbr]
                    for nbr in neighbors
            ]

        if query_node_IDs is None:
            src_ID = node_IDs[ix]
        else:
            src_ID = query_node_IDs[ix]

        io_lib.writeNeighborFileLine(
            stream,
            src_ID,
            mapped_neighbors,
            with_distances=with_distances
        )
        log.tick()
        result = nn_q.get() 
    log.flushTracker()

def _threadedNeighbors(thread_indices, emb_arrs, batch_size, top_k, nn_q, with_distances):
    sess = tf.Session()
    grph = MultiNearestNeighbors(sess, emb_arrs)

    ix = 0
    while ix < len(thread_indices):
        batch = thread_indices[ix:ix+batch_size]
        nn = grph.nearestNeighbors(batch, indices=True, top_k=top_k, no_self=True, with_distances=with_distances)
        for i in range(len(batch)):
            nn_q.put((batch[i], nn[i]))
        ix += batch_size

def _threadedCrossSetNeighbors(thread_indices, src_emb_arrs, dest_emb_arrs, batch_size, top_k, nn_q, with_distances):
    sess = tf.Session()
    grph = MultiNearestNeighbors(sess, dest_emb_arrs)

    ix = 0
    while ix < len(thread_indices):
        batch = thread_indices[ix:ix+batch_size]
        nn = grph.nearestNeighbors(
            [src_emb_arr[batch] for src_emb_arr in src_emb_arrs],
            indices=False,
            top_k=top_k,
            no_self=False,
            with_distances=with_distances
        )
        for i in range(len(batch)):
            nn_q.put((batch[i], nn[i]))
        ix += batch_size

if __name__ == '__main__':
    def _cli():
        import optparse
        parser = optparse.OptionParser(usage='Usage: %prog EMB1 [EMB2 [EMB3 [...]]]')
        parser.add_option('-t', '--threads', dest='threads',
                help='number of threads to use in the computation (min 2, default: %default)',
                type='int', default=2)
        parser.add_option('-o', '--output', dest='outputf',
                help='file to write nearest neighbor results to (default: %default)',
                default='output.csv')
        parser.add_option('--vocab', dest='vocabf',
                help='file to read ordered vocabulary from (will be written if does not exist yet)')
        parser.add_option('-k', '--nearest-neighbors', dest='k',
                help='number of nearest neighbors to calculate (default: %default)',
                type='int', default=25)
        parser.add_option('--batch-size', dest='batch_size',
                type='int', default=25,
                help='number of points to process at once (default %default)')
        parser.add_option('--embedding-mode', dest='embedding_mode',
                type='choice', choices=[pyemblib.Mode.Text, pyemblib.Mode.Binary], default=pyemblib.Mode.Binary,
                help='embedding file is in text ({0}) or binary ({1}) format (default: %default)'.format(pyemblib.Mode.Text, pyemblib.Mode.Binary))
        parser.add_option('--draw-queries-from', dest='draw_queries_from',
                help='comma-separated list of embedding files to use for neighborhood queries,'
                     ' instead of EMB1 EMB2 etc. Queries will still be compared to EMB1 EMB2 etc.'
                     ' If provided, must provide same number of embedding files as provided above.')
        parser.add_option('--partial-neighbors-file', dest='partial_neighbors_file',
                help='file with partially calculated nearest neighbors (for resuming long-running job)')
        parser.add_option('--shared-keys-with', dest='shared_keys_with',
                help='another embedding file; if supplied, nearest neighbor computation'
                     ' will be constrained to those keys shared between EMB1 and this'
                     ' file. (If using --draw-queries-from, will constrain to shared'
                     ' keys between the first of those files and this file instead.)')
        parser.add_option('--filter-to', dest='filter_to',
                help='(optional) file listing keys to filter neighbor calculation to')
        parser.add_option('--filter-queries-to', dest='filter_queries_to',
                help='(optional) file listing query keys to filter neighbor calculation to')
        parser.add_option('--with-distances', dest='with_distances',
                action='store_true', default=False,
                help='include distances in nearest neighbors file')
        parser.add_option('-l', '--logfile', dest='logfile',
                help='name of file to write log contents to (empty for stdout)',
                default=None)
        (options, args) = parser.parse_args()

        if options.draw_queries_from:
            options.draw_queries_from = options.draw_queries_from.split(',')
            if len(options.draw_queries_from) != len(args):
                parser.error('If using --draw-queries-from, must provide same number'
                             ' of embedding files as given on command line!')

        if len(args) < 1:
            parser.print_help()
            exit()
        return args, options

    embedfs, options = _cli()
    log.start(options.logfile)
    log.writeConfig([
        ('Input embedding files', [
            ('Set %d' % (i+1), embedfs[i])
                for i in range(len(embedfs))
        ]),
        ('Input embedding file mode', options.embedding_mode),
        ('Output neighbor file', options.outputf),
        ('Writing distance to neighbors', options.with_distances),
        ('Ordered vocabulary file', options.vocabf),
        ('Number of nearest neighbors', options.k),
        ('Batch size', options.batch_size),
        ('Number of threads', options.threads),
        ('Partial nearest neighbors file for resuming', options.partial_neighbors_file),
        ('Drawing queries from', ('N/A' if not options.draw_queries_from else [
            ('Query set %d' % (i+1), options.draw_queries_from[i])
                for i in range(len(options.draw_queries_from))
        ])),
        ('Restricting to keys shared with', ('N/A' if not options.shared_keys_with else options.shared_keys_with)),
        ('Restricting to keys listed in', ('N/A' if not options.filter_to else options.filter_to)),
        ('Restricting queries to keys listed in', ('N/A' if not options.filter_queries_to else options.filter_queries_to)),
    ], 'k Nearest Neighbor calculation with cosine similarity')

    embeds = []
    for i in range(len(embedfs)):
        t_sub = log.startTimer('Reading embeddings (set %d) from %s...' % (i, embedfs[i]))
        these_embeds = pyemblib.read(embedfs[i], mode=options.embedding_mode, errors='replace')
        log.stopTimer(t_sub, message='Read {0:,} embeddings in {1}s.\n'.format(len(these_embeds), '{0:.2f}'))
        embeds.append(these_embeds)

    query_embeds = None
    if options.draw_queries_from:
        query_embeds = []
        for i in range(len(options.draw_queries_from)):
            t_sub = log.startTimer('Reading query embeddings (set %d) from %s...' % (i, options.draw_queries_from[i]))
            these_embeds = pyemblib.read(options.draw_queries_from[i], mode=options.embedding_mode, errors='replace')
            log.stopTimer(t_sub, message='Read {0:,} embeddings in {1}s.\n'.format(len(these_embeds), '{0:.2f}'))
            query_embeds.append(these_embeds)

    if options.filter_to:
        log.writeln('Reading list of keys to filter to from %s...' % options.filter_to)
        filter_set = io_lib.readSet(options.filter_to, to_lower=True)
        filtered_embed_sets = []
        for emb in embeds:
            filtered_embs = pyemblib.Embeddings()
            for (k,v) in emb.items():
                if k.lower() in filter_set:
                    filtered_embs[k] = v
            filtered_embed_sets.append(filtered_embs)
        embeds = filtered_embed_sets
        log.writeln('  Read set of {0:,} keys'.format(len(filter_set)))
        log.writeln('  Filtered to {0:,} embeddings\n'.format(len(embeds[0])))

    if options.filter_queries_to and options.draw_queries_from:
        log.writeln('Reading list of keys to filter queries to from %s...' % options.filter_queries_to)
        filter_set = io_lib.readSet(options.filter_queries_to, to_lower=True)
        filtered_query_embed_sets = []
        for q_emb in query_embeds:
            filtered_query_embs = pyemblib.Embeddings()
            for (k,v) in q_emb.items():
                if k.lower() in filter_set:
                    filtered_query_embs[k] = v
            filtered_query_embed_sets.append(filtered_query_embs)
        query_embeds = filtered_query_embed_sets
        log.writeln('  Read set of {0:,} query keys'.format(len(filter_set)))
        log.writeln('  Filtered to {0:,} query embeddings\n'.format(len(query_embeds[0])))

    ## TODO: handle this for specified query embeddings
    if options.shared_keys_with:
        t_sub = log.startTimer('Reading reference embeddings from %s...' % options.shared_keys_with)
        emb2 = pyemblib.read(options.shared_keys_with, errors='replace')
        log.stopTimer(t_sub, message='Read {0:,} embeddings in {1}s.\n'.format(len(emb2), '{0:.2f}'))

        if options.filter_to:
            log.writeln('Filtering reference embeddings to filter set...')
            filtered_embs2 = pyemblib.Embeddings()
            for (k,v) in emb2.items():
                if k.lower() in filter_set:
                    filtered_embs2[k] = v
            emb2 = filtered_embs2
            log.writeln('Filtered to {0:,} embeddings\n'.format(len(emb2)))

        log.writeln('Filtering to shared key set...')
        shared_keys = set(embeds[0].keys()).intersection(set(emb2.keys()))
        filtered_embed_sets = []
        for emb in embeds:
            filtered_emb = pyemblib.Embeddings()
            for key in shared_keys:
                filtered_emb[key] = emb[key]
            filtered_embed_sets.append(filtered_emb)
            embeds = filtered_embed_sets
        log.writeln('Filtered to {0:,} embeddings.\n'.format(len(embeds[0])))

    if not os.path.isfile(options.vocabf):
        log.writeln('Writing node ID <-> vocab map to %s...\n' % options.vocabf)
        io_lib.writeNodeMap(embeds[0], options.vocabf)
    else:
        log.writeln('Reading node ID <-> vocab map from %s...\n' % options.vocabf)
    node_map = io_lib.readNodeMap(options.vocabf)

    if options.draw_queries_from:
        query_vocabf = '%s.query' % options.vocabf
        if not os.path.isfile(query_vocabf):
            log.writeln('Writing query node ID <-> vocab map to %s...\n' % query_vocabf)
            io_lib.writeNodeMap(query_embeds[0], query_vocabf)
        else:
            log.writeln('Reading query node ID <-> vocab map from %s...\n' % query_vocabf)
        query_node_map = io_lib.readNodeMap(query_vocabf)

    # get the vocabulary in node ID order, and map index in emb_arr
    # to node IDs
    node_IDs = list(node_map.keys())
    node_IDs.sort()
    ordered_vocab = [
        node_map[node_ID]
            for node_ID in node_IDs
    ]

    emb_arrs = []
    for i in range(len(embeds)):
        emb_arr = np.array([
            embeds[i][v] for v in ordered_vocab
        ])
        emb_arrs.append(emb_arr)
    
    # do the same setup for query embedding arrays
    if options.draw_queries_from:
        query_node_IDs = list(query_node_map.keys())
        query_node_IDs.sort()
        ordered_query_vocab = [
            query_node_map[query_node_ID]
                for query_node_ID in query_node_IDs
        ]

        query_emb_arrs = []
        for i in range(len(query_embeds)):
            query_emb_arr = np.array([
                query_embeds[i][v] for v in ordered_query_vocab
            ])
            query_emb_arrs.append(query_emb_arr)

    # TODO: what should this do if a query set is specified?
    if options.partial_neighbors_file:
        completed_neighbors = set()
        with open(options.partial_neighbors_file, 'r') as stream:
            for line in stream:
                if line[0] != '#':
                    (neighbor_id, _) = line.split(',', 1)
                    completed_neighbors.add(int(neighbor_id))
    else:
        completed_neighbors = set()

    log.writeln('Calculating k nearest neighbors.')
    if options.draw_queries_from:
        KNearestNeighborsFromQueries(
            emb_arrs,
            node_IDs,
            query_emb_arrs,
            query_node_IDs,
            options.k,
            options.outputf,
            threads=options.threads,
            batch_size=options.batch_size,
            completed_neighbors=completed_neighbors,
            with_distances=options.with_distances
        )
    else:
        KNearestNeighbors(
            emb_arrs,
            node_IDs,
            options.k,
            options.outputf,
            threads=options.threads,
            batch_size=options.batch_size,
            completed_neighbors=completed_neighbors,
            with_distances=options.with_distances
        )
    log.writeln('Done!\n')

    log.stop()
