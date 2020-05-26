#!/usr/bin/env python2

import sys
import os
import socket
import struct
import math
import re
import time
import subprocess
import pickle
import multiprocessing

from tempfile import NamedTemporaryFile, mkdtemp
import uuid
from collections import defaultdict, Counter

from .hmmer_seqio import iter_fasta_seqs
from ..annotation import annota
from ..common import *


SCANTYPE_MEM = "mem"
SCANTYPE_DISK = "disk"

QUERY_TYPE_SEQ = "seq"
QUERY_TYPE_HMM = "hmm"

DB_TYPE_SEQ = "seqdb"
DB_TYPE_HMM = "hmmdb"

B62_IDENTITIES = {'A': 4, 'B': 4, 'C': 9, 'D': 6, 'E': 5, 'F': 6, 'G': 6, 'H': 8,
                  'I': 4, 'K': 5, 'L': 4, 'M': 5, 'N': 6, 'P': 7, 'Q': 5, 'R': 5,
                  'S': 4, 'T': 5, 'V': 4, 'W': 11, 'X': -1, 'Y': 7, 'Z': 4}


def iter_hits(source, translate, query_type, dbtype, scantype, host, port,
              evalue_thr=None, score_thr=None, max_hits=None, return_seq=False,
              skip=None, maxseqlen=None, fixed_Z=None, qcov_thr=None, cpus=1,
              base_tempdir=None):

    try:
        max_hits = int(max_hits)
        if max_hits == 0: # unlimited hits
            max_hits = None
    except Exception:
        max_hits = None

    print("hmmer_search.py:iter_hits")

    if scantype == SCANTYPE_MEM and query_type == QUERY_TYPE_SEQ:
        return iter_seq_hits(source, translate, host, port, dbtype=dbtype, evalue_thr=evalue_thr, score_thr=score_thr, max_hits=max_hits, skip=skip, maxseqlen=maxseqlen)
    
    elif scantype == SCANTYPE_MEM and query_type == QUERY_TYPE_HMM and dbtype == DB_TYPE_SEQ:
        return iter_hmm_hits(source, host, port, dbtype=dbtype, evalue_thr=evalue_thr, score_thr=score_thr, max_hits=max_hits, skip=skip, maxseqlen=maxseqlen, fixed_Z=fixed_Z)
    
    elif scantype == SCANTYPE_DISK and query_type == QUERY_TYPE_SEQ:
        return hmmscan(source, translate, host, evalue_thr=evalue_thr, score_thr=score_thr, max_hits=max_hits, cpus=cpus, maxseqlen=maxseqlen, base_tempdir=base_tempdir)
    
    else:
        raise ValueError('not supported')

    
def safe_cast(v):
    try:
        return float(v)
    except ValueError:
        return v.strip()


def unpack_hit(bindata, z):
    (name, acc, desc, window_length, sort_key, score, pre_score, sum_score,
     pvalue, pre_pvalue, sum_pvalue, nexpected, nregions, nclustered,
     noverlaps, nenvelopes, ndom, flags, nreported, nincluded, best_domain,
     seqidx, subseq_start, dcl, offset) = struct.unpack("3Q I 4x d 3f 4x 3d f 9I 4Q", bindata)

    evalue = math.exp(pvalue) * z
    return name, evalue, sum_score, ndom


def unpack_stats(bindata):
    (elapsed, user, sys, Z, domZ, Z_setby, domZ_setby, nmodels, nseqs,
     n_past_msv, n_past_bias, n_past_vit, n_past_fwd, nhits, nreported,
     nincluded) = struct.unpack("5d 2I 9q", bindata)

    return elapsed, nhits, Z, domZ


def scan_hits(data, address="127.0.0.1", port=51371, evalue_thr=None,
              score_thr=None, max_hits=None, fixed_Z=None):

    print("hmmer_search.py:scan_hits")
    # print(data)
    
    s = socket.socket()
    try:
        s.connect((address, port))
    except Exception as e:
        print(address, port, e)
        raise
    s.sendall(data.encode())

    status = s.recv(16)
    st, msg_len = struct.unpack("I 4x Q", status)
    elapsed, nreported = 0, 0
    hits = defaultdict(list)
    if st == 0:
        binresult = b''
        while len(binresult) < msg_len:
            binresult += s.recv(4096)

        elapsed, nreported, Z, domZ = unpack_stats(binresult[0:120])
        if fixed_Z:
            Z = fixed_Z

        hitdata = defaultdict(dict)

        hits_start = 120 # First 120 bits are the stats
        hits_end = hits_start

        for hitid in range(nreported):
            hits_end = hits_start + 152
            name, evalue, score, ndom = unpack_hit(binresult[hits_start:hits_end], Z)
            hitdata[hitid] = {"name": name, "evalue": evalue, "score":score, "ndom": ndom, "doms": []}

            print("hmmer_search.py:scan_hits HIT reported")
            print(hitid)
            print(hitdata[hitid])
            hits_start += 152

        next_start = hits_end
        reported_hits = []
        for hitid in range(nreported):
            hit = hitdata[hitid]
            for domid in range(hit["ndom"]):
                dombit = binresult[next_start:next_start + 72]
                dom = struct.unpack("4i 5f 4x d 2i Q 8x", dombit)
                is_reported = dom[10]
                is_included = dom[11]
                hit["doms"].append(dom)
                next_start += 72

            print("hmmer_search.py:scan_hits after doms")
            print(hit)
            
            for domid in range(hit["ndom"]):
                alibit = struct.unpack("7Q I 4x 3Q 3I 4x 6Q I 4x Q", binresult[next_start:next_start+168])

                (rfline, mmline, csline, model, mline, aseq, ppline, N,
                hmmname, hmmacc, hmmdesc, hmmfrom, hmmto, M, sqname, sqacc,
                sqdesc,sqfrom, sqto, L, memsize, mem) = alibit
                next_start += 168
                # Skip alignment
                # ....
                next_start += (memsize)
                d = hit["doms"][domid]
                bitscore = d[8]
                ievalue = math.exp(d[9] * Z)
                cevalue = math.exp(d[9] * domZ)
                evalue = hit["evalue"]
                score = hit["score"]

                print("hmmer_search.py:scan_hits check thresholds")
                print((evalue_thr is None or evalue <= evalue_thr))
                print((score_thr is not None and score >= score_thr))
                
                if (evalue_thr is None or evalue <= evalue_thr) and \
                    (score_thr is None or score >= score_thr):
                    
                    reported_hits.append((hit["name"], hit["evalue"], hit["score"], hmmfrom,
                             hmmto, sqfrom, sqto, bitscore))

            if max_hits is not None and (hitid+1) == max_hits:
                break
    else:
        ret = s.recv(4096).decode().strip()
        s.close()        
        raise ValueError('hmmpgmd error: %s' % ret)

    s.close()

    print("hmmer_search.py:scan_hits return")
    print(elapsed)
    print(reported_hits)
    
    return elapsed, reported_hits


def iter_hmm_hits(hmmfile, host, port, dbtype=DB_TYPE_HMM,
                  evalue_thr=None, score_thr=None,
                  max_hits=None, skip=None, maxseqlen=None, fixed_Z=None):

    print("hmmer_search.py:iter_hmm_hits")
    
    hmmer_version = None
    model = ''
    name = 'Unknown'
    leng = None
    with open(hmmfile) as HMMFILE:
        for line in HMMFILE:

            if hmmer_version is None:
                hmmer_version = line
                
            if line.startswith("NAME"):
                name = line.split()[-1]
                model = ''
                leng = None
            if line.startswith("LENG"):
                leng = int(line.split()[-1])
                
            model += line
            if line.strip() == '//':
                if skip and name in skip:
                    continue
                else:                    
                    data = f'@--{dbtype} 1\n{hmmer_version}\n{model}'

                    print("hmmer_search.py:iter_hmm_hits call scan_hist")
                    print(str(name) + " - " + str(leng))
                    # print(data)

                    etime, hits = scan_hits(data, host, port,
                                            evalue_thr=evalue_thr, score_thr=score_thr,
                                            max_hits=max_hits, fixed_Z=fixed_Z)

                    yield name, etime, hits, leng, None


# HMMFILE.tell() nor working
# OSError: telling position disabled by next() call
# def iter_hmm_hits(hmmfile, host, port, dbtype=DB_TYPE_HMM, evalue_thr=None,
#                   max_hits=None, skip=None, maxseqlen=None, fixed_Z=None):

#     HMMFILE = open(hmmfile)
#     with open(hmmfile) as HMMFILE:
#         while HMMFILE.tell() != os.fstat(HMMFILE.fileno()).st_size:
#             model = ''
#             name = 'Unknown'
#             leng = None
#             for line in HMMFILE:
#                 if line.startswith("NAME"):
#                     name = line.split()[-1]
#                 if line.startswith("LENG"):
#                     hmm_leng = int(line.split()[-1])
#                 model += line
#                 if line.strip() == '//':
#                     break

#             if skip and name in skip:
#                 continue

#             data = '@--%s 1\n%s' % (dbtype, model)
            
#             # print("iter_hmm_hits")
#             # print(data)
            
#             etime, hits = scan_hits(data, host, port, evalue_thr=evalue_thr,
#                                     max_hits=max_hits, fixed_Z=fixed_Z)
            
#             yield name, etime, hits, hmm_leng, None


def iter_seq_hits(src, translate, host, port, dbtype, evalue_thr=None,
                  score_thr=None, max_hits=None, maxseqlen=None, fixed_Z=None,
                  skip=None):

    for seqnum, (name, seq) in enumerate(iter_fasta_seqs(src, translate=translate)):
        if skip and name in skip:
            continue

        if maxseqlen and len(seq) > maxseqlen:
            yield name, -1, [], len(seq), None
            continue

        if not seq:
            continue

        seq = re.sub("-.", "", seq)
        data = '@--%s 1\n>%s\n%s\n//' % (dbtype, name, seq)
        etime, hits = scan_hits(data, host, port, evalue_thr=evalue_thr,
                                score_thr=score_thr, max_hits=max_hits,
                                fixed_Z=fixed_Z)

        #max_score = sum([B62_IDENTITIES.get(nt, 0) for nt in seq])
        yield name, etime, hits, len(seq), None


def get_hits(name, record, address="127.0.0.1", port=51371, dbtype=DB_TYPE_HMM, qtype=QUERY_TYPE_SEQ,
             evalue_thr=None, score_thr = None, max_hits=None):

    if qtype == QUERY_TYPE_SEQ:
        seq = re.sub("-.", "", record)
        data = f'@--{dbtype} 1\n>{name}\n{seq}\n//'
    elif qtype == QUERY_TYPE_HMM:
        data = f'@--{dbtype} 1\n{record}\n//'        
    else:
        raise Exception(f"Unrecognized query type {qtype}.")
    
    etime, hits = scan_hits(data, address=address, port=port,
                            evalue_thr=evalue_thr, score_thr=score_thr, max_hits=max_hits)

    print("hmmer_search.py:get_hits")
    print(name)
    print(etime)
    print(hits)
    
    return name, etime, hits


def hmmscan(query_file, translate, database_path, cpus=1, evalue_thr=None,
            score_thr=None, max_hits=None, fixed_Z=None, maxseqlen=None,
            base_tempdir=None):
    if not HMMSCAN:
        raise ValueError('hmmscan not found in path')

    tempdir = mkdtemp(prefix='emappertmp_hmmscan_', dir=base_tempdir)

    OUT = NamedTemporaryFile(dir=tempdir, mode='w+')
    if translate or maxseqlen:
        if translate:
            print('translating query input file')
        Q = NamedTemporaryFile(mode='w')
        for name, seq in iter_fasta_seqs(query_file, translate=translate):
            if maxseqlen is None or len(seq) <= maxseqlen:
                print(f">{name}\n{seq}", file=Q)
                # Q.write(f">{name}\n{seq}".encode())
        Q.flush()
        query_file = Q.name

    cmd = '%s --cpu %s -o /dev/null --domtblout %s %s %s' % (
        HMMSCAN, cpus, OUT.name, database_path, query_file)
    # print '#', cmd
    # print cmd
    sts = subprocess.call(cmd, shell=True)
    byquery = defaultdict(list)

    last_query = None
    last_hitname = None
    hit_list = []
    hit_ids = set()
    last_query_len = None
    if sts == 0:
        for line in OUT:
            # TBLOUT
            # ['#', '---', 'full', 'sequence', '----', '---', 'best', '1', 'domain', '----', '---', 'domain', 'number', 'estimation', '----']
            # ['#', 'target', 'name', 'accession', 'query', 'name', 'accession', 'E-value', 'score', 'bias', 'E-value', 'score', 'bias', 'exp', 'reg', 'clu', 'ov', 'env', 'dom', 'rep', 'inc', 'description', 'of', 'target']
            # ['#-------------------', '----------', '--------------------', '----------', '---------', '------', '-----', '---------', '------', '-----', '---', '---', '---', '---', '---', '---', '---', '---', '---------------------']
            # ['delNOG20504', '-', '553220', '-', '1.3e-116', '382.9', '6.2', '3.4e-116', '381.6', '6.2', '1.6', '1', '1', '0', '1', '1', '1', '1', '-']
            # fields = line.split() # output is not tab delimited! Should I trust this split?
            # hit, _, query, _ , evalue, score, bias, devalue, dscore, dbias = fields[0:10]

            # DOMTBLOUT
            #                                                                             --- full sequence --- -------------- this domain -------------   hmm coord   ali coord   env coord
            # target name        accession   tlen query name            accession   qlen   E-value  score  bias   #  of  c-Evalue  i-Evalue  score  bias  from    to  from    to  from    to  acc description of target
            # ------------------- ---------- -----  -------------------- -------
            # Pkinase              PF00069.22   264 1000565.METUNv1_02451 -
            # 858   4.5e-53  180.2   0.0   1   1   2.4e-56   6.6e-53  179.6
            # 0.0     1   253   580   830   580   838 0.89 Protein kinase
            # domain
            if line.startswith('#'):
                continue
            fields = line.split()

            (hitname, hacc, tlen, qname, qacc, qlen, evalue, score, bias, didx,
             dnum, c_evalue, i_evalue, d_score, d_bias, hmmfrom, hmmto, seqfrom,
             seqto, env_from, env_to, acc) = list(map(safe_cast, fields[:22]))

            if (last_query and qname != last_query):
                yield last_query, 0, hit_list, last_query_len, None
                hit_list = []
                hit_ids = set()
                last_query = qname
                last_query_len = None

            last_query = qname
            if last_query_len and last_query_len != qlen:
                raise ValueError(
                    "Inconsistent qlen when parsing hmmscan output")
            last_query_len = qlen

            if (evalue_thr is None or evalue <= evalue_thr) and \
               (score_thr is not None and score >= score_thr) and \
               (max_hits is None or last_hitname == hitname or len(hit_ids) < max_hits):

                hit_list.append([hitname, evalue, score, hmmfrom,
                                 hmmto, seqfrom, seqto, d_score])
                hit_ids.add(hitname)
                last_hitname = hitname

        if last_query:
            yield last_query, 0, hit_list, last_query_len, None

    OUT.close()
    if translate:
        Q.close()
    shutil.rmtree(tempdir)

def hmmsearch(query_hmm, target_db, cpus=1):
    if not HMMSEARCH:
        raise ValueError('hmmsearch not found in path')

    OUT = NamedTemporaryFile()
    cmd = '%s --cpu %s -o /dev/null -Z 1000000 --tblout %s %s %s' % (
        HMMSEARCH, cpus, OUT.name, query_hmm, target_db)

    sts = subprocess.call(cmd, shell=True)
    byquery = defaultdict(list)
    if sts == 0:
        for line in OUT:
            #['#', '---', 'full', 'sequence', '----', '---', 'best', '1', 'domain', '----', '---', 'domain', 'number', 'estimation', '----']
            #['#', 'target', 'name', 'accession', 'query', 'name', 'accession', 'E-value', 'score', 'bias', 'E-value', 'score', 'bias', 'exp', 'reg', 'clu', 'ov', 'env', 'dom', 'rep', 'inc', 'description', 'of', 'target']
            #['#-------------------', '----------', '--------------------', '----------', '---------', '------', '-----', '---------', '------', '-----', '---', '---', '---', '---', '---', '---', '---', '---', '---------------------']
            #['delNOG20504', '-', '553220', '-', '1.3e-116', '382.9', '6.2', '3.4e-116', '381.6', '6.2', '1.6', '1', '1', '0', '1', '1', '1', '1', '-']
            if line.startswith('#'):
                continue
            fields = line.split()  # output is not tab delimited! Should I trust this split?
            hit, _, query, _, evalue, score, bias, devalue, dscore, dbias = fields[0:10]
            evalue, score, bias, devalue, dscore, dbias = list(map(
                float, [evalue, score, bias, devalue, dscore, dbias]))
            byquery[query].append([query, evalue, score])

    OUT.close()
    return byquery

# refine orthologs using phmmer


def refine_hit(args):
    seqname, seq, group_fasta, excluded_taxa, tempdir = args
    F = NamedTemporaryFile(delete=True, dir=tempdir, mode='w+')
    F.write(f'>{seqname}\n{seq}')
    F.flush()

    best_hit = get_best_hit(F.name, group_fasta, excluded_taxa, tempdir)
    F.close()

    return [seqname] + best_hit


def get_best_hit(target_seq, target_og, excluded_taxa, tempdir):
    if not PHMMER:
        raise ValueError('phmmer not found in path')

    tempout = pjoin(tempdir, uuid.uuid4().hex)
    cmd = "%s --incE 0.001 -E 0.001 -o /dev/null --noali --tblout %s %s %s" %\
          (PHMMER, tempout, target_seq, target_og)

    # print cmd
    status = os.system(cmd)
    best_hit_found = False
    if status == 0:
        # take the best hit
        for line in open(tempout):
            if line.startswith('#'):
                continue
            else:
                fields = line.split()
                best_hit_name = fields[0]
                best_hit_evalue = float(fields[4])
                best_hit_score = float(fields[5])
                if not excluded_taxa or not best_hit_name.startswith("%s." % (excluded_taxa)):
                    best_hit_found = True
                    break
        os.remove(tempout)
    else:
        raise ValueError('Error running PHMMER')

    if not best_hit_found:
        best_hit_evalue = '-'
        best_hit_score = '-'
        best_hit_name = '-'

    return [best_hit_name, best_hit_evalue, best_hit_score]
