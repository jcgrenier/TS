#!/usr/bin/python
# Copyright (C) 2012 Ion Torrent Systems, Inc. All Rights Reserved

from ion.utils.blockprocessing import printtime

import subprocess
import os
import sys
import traceback
import json
import re
import time
import dateutil
from shutil import move
import math
import shlex
import copy

from ion.utils import TFPipeline
from ion.utils.blockprocessing import isbadblock
from ion.reports import MaskMerge

from ion.reports import mergeBaseCallerJson
from ion.utils import blockprocessing
from ion.utils import ionstats

from ion.utils import ionstats_plots

def basecaller_cmd(basecallerArgs,
                   SIGPROC_RESULTS,
                   libKey,
                   tfKey,
                   runID,
                   BASECALLER_RESULTS,
                   block_col_offset,
                   block_row_offset,
                   datasets_pipeline_path,
                   adapter):
    if basecallerArgs:
        cmd = basecallerArgs
    else:
        cmd = "BaseCaller"
        printtime("ERROR: BaseCaller command not specified, using default: 'BaseCaller'")
    
    cmd += " --input-dir=%s" % (SIGPROC_RESULTS)
    cmd += " --librarykey=%s" % (libKey)
    cmd += " --tfkey=%s" % (tfKey)
    cmd += " --run-id=%s" % (runID)
    cmd += " --output-dir=%s" % (BASECALLER_RESULTS)
    cmd += " --block-col-offset %d" % (block_col_offset)
    cmd += " --block-row-offset %d" % (block_row_offset)
    cmd += " --datasets=%s" % (datasets_pipeline_path)
    cmd += " --trim-adapter %s" % (adapter)

    return cmd


def basecalling(
      SIGPROC_RESULTS,
      basecallerArgs,
      libKey,
      tfKey,
      runID,
      reverse_primer_dict,
      BASECALLER_RESULTS,
      barcodeId,
      barcodeInfo,
      library,
      notes,
      site_name,
      platform,
      instrumentName,
      chipType,
      ):

    if not os.path.exists(BASECALLER_RESULTS):
        os.mkdir(BASECALLER_RESULTS)


    ''' Step 1: Generate datasets_pipeline.json '''

    # New file, datasets_pipeline.json, contains the list of all active result files.
    # Tasks like post_basecalling, alignment, plugins, must process each specified file and merge results
    
    datasets_pipeline_path = os.path.join(BASECALLER_RESULTS,"datasets_pipeline.json")

    try:
        generate_datasets_json(
            barcodeId,
            barcodeInfo,
            library,
            runID,
            notes,
            site_name,
            platform,
            instrumentName,
            chipType,
            datasets_pipeline_path,
        )
    except:
        printtime('ERROR: Generation of barcode_files.json unsuccessful')
        traceback.print_exc()



    ''' Step 2: Invoke BaseCaller '''

    try:
        [(x,y)] = re.findall('block_X(.*)_Y(.*)',os.getcwd())
        if x.isdigit():
            block_col_offset = int(x)
        else:
            block_col_offset = 0

        if y.isdigit():
            block_row_offset = int(y)
        else:
            block_row_offset = 0
    except:
        block_col_offset = 0
        block_row_offset = 0

    try:
        # 3' adapter details
        adapter = reverse_primer_dict['sequence']
        # TODO: provide barcode_filter via datasets.json

        cmd = basecaller_cmd(basecallerArgs,
                             SIGPROC_RESULTS,
                             libKey,
                             tfKey,
                             runID,
                             BASECALLER_RESULTS,
                             block_col_offset,
                             block_row_offset,
                             datasets_pipeline_path,
                             adapter)

        printtime("DEBUG: Calling '%s':" % cmd)
        proc = subprocess.Popen(shlex.split(cmd.encode('utf8')), shell=False, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        stdout_value, stderr_value = proc.communicate()
        ret = proc.returncode
        sys.stdout.write("%s" % stdout_value)
        sys.stderr.write("%s" % stderr_value)

        # Ion Reporter
        try:
            basecaller_log_path = os.path.join(BASECALLER_RESULTS, 'basecaller.log')
            with open(basecaller_log_path, 'a') as f:
                if stdout_value: f.write(stdout_value)
                if stderr_value: f.write(stderr_value)
        except IOError:
            traceback.print_exc()

        if ret != 0:
            printtime('ERROR: BaseCaller failed with exit code: %d' % ret)
            raise
        #ignore rest of operations
        if '--calibration-training' in basecallerArgs:
            printtime('training mode: ignore filtering')
            return
    except:
        printtime('ERROR: BaseCaller failed')
        traceback.print_exc()
        raise


    printtime("Finished basecaller processing")






def merge_barcoded_basecaller_bams(BASECALLER_RESULTS, basecaller_datasets, method):

    try:
        composite_bam_filename = os.path.join(BASECALLER_RESULTS,'rawlib.basecaller.bam')
        if not os.path.exists(composite_bam_filename): #TODO

            bam_file_list = []
            for dataset in basecaller_datasets["datasets"]:
                print os.path.join(BASECALLER_RESULTS, dataset['basecaller_bam'])
                if os.path.exists(os.path.join(BASECALLER_RESULTS, dataset['basecaller_bam'])):
                    bam_file_list.append(os.path.join(BASECALLER_RESULTS, dataset['basecaller_bam']))

            composite_bai_filepath=""
            mark_duplicates=False
            blockprocessing.merge_bam_files(bam_file_list, composite_bam_filename, composite_bai_filepath, mark_duplicates, method)
    except:
        traceback.print_exc()
        printtime("ERROR: Generate merged %s on barcoded run failed" % composite_bam_filename)

    printtime("Finished basecaller barcode merging")


def merge_datasets_basecaller_json(dirs, BASECALLER_RESULTS):

    ########################################################
    # Merge datasets_basecaller.json                       #
    ########################################################
    
    block_datasets_json = []
    combined_datasets_json = {}
    
    for dir in dirs:
        current_datasets_path = os.path.join(dir,BASECALLER_RESULTS,'datasets_basecaller.json')
        try:
            f = open(current_datasets_path,'r')
            block_datasets_json.append(json.load(f))
            f.close()
        except:
            printtime("ERROR: skipped %s" % current_datasets_path)
    
    if (not block_datasets_json) or ('datasets' not in block_datasets_json[0]) or ('read_groups' not in block_datasets_json[0]):
        printtime("merge_basecaller_results: no block contained a valid datasets_basecaller.json, aborting")
        return

    combined_datasets_json = copy.deepcopy(block_datasets_json[0])
    
    for dataset_idx in range(len(combined_datasets_json['datasets'])):
        combined_datasets_json['datasets'][dataset_idx]['read_count'] = 0
        for current_datasets_json in block_datasets_json:
            combined_datasets_json['datasets'][dataset_idx]['read_count'] += current_datasets_json['datasets'][dataset_idx].get("read_count",0)
    
    for read_group in combined_datasets_json['read_groups'].iterkeys():
        combined_datasets_json['read_groups'][read_group]['Q20_bases'] = 0
        combined_datasets_json['read_groups'][read_group]['total_bases'] = 0
        combined_datasets_json['read_groups'][read_group]['read_count'] = 0
        combined_datasets_json['read_groups'][read_group]['filtered'] = True if 'barcode_sequence' in combined_datasets_json['read_groups'][read_group] else False
        if "barcode_sequence" in combined_datasets_json['read_groups'][read_group]:
            combined_datasets_json['read_groups'][read_group]['barcode_bias'] = [-1]
            combined_datasets_json['read_groups'][read_group]['barcode_distance_hist'] = [0,0,0,0,0]
            combined_datasets_json['read_groups'][read_group]['barcode_errors_hist'] = [0,0,0]
            combined_datasets_json['read_groups'][read_group]['barcode_match_filtered'] = 0
            combined_datasets_json['read_groups'][read_group]['num_blocks_filtered'] = 0
                  
        for current_datasets_json in block_datasets_json:
            combined_datasets_json['read_groups'][read_group]['Q20_bases'] += current_datasets_json['read_groups'].get(read_group,{}).get("Q20_bases",0)
            combined_datasets_json['read_groups'][read_group]['total_bases'] += current_datasets_json['read_groups'].get(read_group,{}).get("total_bases",0)
            combined_datasets_json['read_groups'][read_group]['read_count'] += current_datasets_json['read_groups'].get(read_group,{}).get("read_count",0)
            combined_datasets_json['read_groups'][read_group]['filtered'] &= current_datasets_json['read_groups'].get(read_group,{}).get("filtered",True)
            if current_datasets_json['read_groups'].get(read_group,{}).get("filtered",False):
                combined_datasets_json['read_groups'][read_group]['num_blocks_filtered'] += 1
            if "barcode_sequence" in combined_datasets_json['read_groups'][read_group]:
                combined_datasets_json['read_groups'][read_group]['barcode_match_filtered'] += current_datasets_json['read_groups'].get(read_group,{}).get("barcode_match_filtered",0)
                error_hist = current_datasets_json['read_groups'].get(read_group,{}).get("barcode_errors_hist",[0,0,0])
                for hist_idx in range(len(error_hist)):
                    combined_datasets_json['read_groups'][read_group]['barcode_errors_hist'][hist_idx] += error_hist[hist_idx]
                distance_hist = current_datasets_json['read_groups'].get(read_group,{}).get("barcode_distance_hist",[0,0,0,0,0])
                for hist_idx in range(len(distance_hist)):
                    combined_datasets_json['read_groups'][read_group]['barcode_distance_hist'][hist_idx] += distance_hist[hist_idx]
                barcode_bias = current_datasets_json['read_groups'].get(read_group,{}).get("barcode_bias",[-1])
                if (combined_datasets_json['read_groups'][read_group]['barcode_bias'] == [-1]) & (barcode_bias != [-1]):
                    combined_datasets_json['read_groups'][read_group]['barcode_bias'] = len(barcode_bias) * [0]
                if barcode_bias != [-1]:
                    for bias_idx in range(len(barcode_bias)):
                        combined_datasets_json['read_groups'][read_group]['barcode_bias'][bias_idx] += barcode_bias[bias_idx] * current_datasets_json['read_groups'].get(read_group,{}).get("read_count",0)
        # After combining all the blocks
        if "barcode_sequence" in combined_datasets_json['read_groups'][read_group]:
            if combined_datasets_json['read_groups'][read_group]['barcode_bias'] == [-1]:
                combined_datasets_json['read_groups'][read_group]['barcode_bias'] = [0]
            if combined_datasets_json['read_groups'][read_group]['read_count'] > 0:
                for bias_idx in range(len(combined_datasets_json['read_groups'][read_group]['barcode_bias'])):
                    combined_datasets_json['read_groups'][read_group]['barcode_bias'][bias_idx] /= combined_datasets_json['read_groups'][read_group]['read_count']

    # Barcode filters -------------------------------------------------------
    # Potential filters 1) frequency filter 2) minreads filter 3) error histogram filter
    # No use to attempt filtering here if filtering is done per block or json entries are missing
    if "barcode_filters" in combined_datasets_json and (combined_datasets_json['barcode_filters']['filter_postpone'] != 0):
        # Loop through read groups to compute combined filtering threshold
        max_reads = 0
        for read_group in combined_datasets_json['read_groups'].iterkeys():
            if "barcode_sequence" in combined_datasets_json['read_groups'][read_group]:
                max_reads = max(max_reads, combined_datasets_json['read_groups'][read_group]['read_count'])
        filter_threshold = combined_datasets_json['barcode_filters']['filter_minreads']
        filter_threshold = max(filter_threshold, math.floor(max_reads*combined_datasets_json['barcode_filters']['filter_frequency']))
        
        # Doing the actual filtering - exclude no-match read group
        for read_group in combined_datasets_json['read_groups']:
            filter_me = (combined_datasets_json['read_groups'][read_group]['sample'] == 'none')
            if ("barcode_sequence" in combined_datasets_json['read_groups'][read_group]) and filter_me:
                if combined_datasets_json['read_groups'][read_group]['read_count'] <= filter_threshold:
                    combined_datasets_json['read_groups'][read_group]['filtered'] = True
                if (not combined_datasets_json['read_groups'][read_group]['filtered']) and (combined_datasets_json['barcode_filters']['filter_errors_hist'] > 0):
                    av_errors = (combined_datasets_json['read_groups'][read_group]['barcode_errors_hist'][1] + 2*combined_datasets_json['read_groups'][read_group]['barcode_errors_hist'][2]) / combined_datasets_json['read_groups'][read_group]['read_count']
                    combined_datasets_json['read_groups'][read_group]['filtered'] = (av_errors > combined_datasets_json['barcode_filters']['filter_errors_hist'])
    # ----------------------------------------------------------------------
        
    try:
        f = open(os.path.join(BASECALLER_RESULTS,'datasets_basecaller.json'),"w")
        json.dump(combined_datasets_json, f, indent=4)
        f.close()
    except:
        printtime("ERROR: Failed to write merged datasets_basecaller.json")
        traceback.print_exc()


def merge_basecaller_stats(dirs, BASECALLER_RESULTS):

    merge_datasets_basecaller_json(dirs, BASECALLER_RESULTS)

    ########################################################
    # write composite return code                          #
    ########################################################

    try:
        if len(dirs)==96:
            composite_return_code=96
            for subdir in dirs:

                blockstatus_return_code_file = os.path.join(subdir,"blockstatus.txt")
                if os.path.exists(blockstatus_return_code_file):

                    with open(blockstatus_return_code_file, 'r') as f:
                        text = f.read()
                        if 'Basecaller=0' in text:
                            composite_return_code-=1
                        else:
                            with open(os.path.join(subdir,"sigproc_results","analysis_return_code.txt"), 'r') as g:
                                return_code_text = g.read()
                                if return_code_text=="3" and subdir in ['block_X0_Y0','block_X14168_Y0','block_X0_Y9324','block_X14168_Y9324']:
                                    printtime("INFO: suppress non-critical error in %s" % subdir)
                                    composite_return_code-=1
                                    

            composite_return_code_file = os.path.join(BASECALLER_RESULTS,"composite_return_code.txt")
            if not os.path.exists(composite_return_code_file):
                printtime("DEBUG: create %s" % composite_return_code_file)
                os.umask(0002)
                f = open(composite_return_code_file, 'a')
                f.write(str(composite_return_code))
                f.close()
            else:
                printtime("DEBUG: skip generation of %s" % composite_return_code_file)
    except:
        traceback.print_exc()


    ###############################################
    # Merge BaseCaller.json files                 #
    ###############################################
    printtime("Merging BaseCaller.json files")

    try:
        basecallerfiles = []
        for subdir in dirs:
            subdir = os.path.join(BASECALLER_RESULTS,subdir)
            printtime("DEBUG: %s:" % subdir)
            if isbadblock(subdir, "Merging BaseCaller.json files"):
                continue
            basecallerjson = os.path.join(subdir,'BaseCaller.json')
            if os.path.exists(basecallerjson):
                basecallerfiles.append(subdir)
            else:
                printtime("ERROR: Merging BaseCaller.json files: skipped %s" % basecallerjson)

        mergeBaseCallerJson.merge(basecallerfiles,BASECALLER_RESULTS)
    except:
        traceback.print_exc()
        printtime("Merging BaseCaller.json files failed")

    printtime("Finished merging basecaller stats")


def merge_bams(dirs, BASECALLER_RESULTS, basecaller_datasets, method):

    for dataset in basecaller_datasets['datasets']:

        try:
            bamdir = BASECALLER_RESULTS
            bamfile = dataset['basecaller_bam']
            block_bam_list = [os.path.join(blockdir, bamdir, bamfile) for blockdir in dirs]
            block_bam_list = [block_bam_filename for block_bam_filename in block_bam_list if os.path.exists(block_bam_filename)]
            composite_bam_filepath = os.path.join(bamdir, bamfile)
            if block_bam_list:
                composite_bai_filepath=""
                mark_duplicates=False
                blockprocessing.merge_bam_files(block_bam_list, composite_bam_filepath, composite_bai_filepath, mark_duplicates, method)
        except:
            traceback.print_exc()
            printtime("ERROR: merging %s unsuccessful" % bamfile)

    printtime("Finished merging basecaller BAM files")



def generate_datasets_json(
        barcodeId,
        barcodeInfo,
        library,
        runID,
        notes,
        site_name,
        platform,
        instrumentName,
        chipType,
        datasets_json_path
        ):

    # TS-6135: ignore optional LB field, TODO: track library in database

    if not site_name:
        site_name = ""
    if not notes:
        notes = ""
    
    datasets = {
        "meta" : {
            "format_name"       : "Dataset Map",
            "format_version"    : "1.0",
            "generated_by"      : "basecaller.py",
            "creation_date"     : dateutil.parser.parse(time.asctime()).isoformat()
        },
        "sequencing_center" :  "%s/%s" % (''.join(ch for ch in site_name if ch.isalnum()), instrumentName),
        "datasets" : [],
        "read_groups" : {}
    }
    
    # get no barcode sample name and reference
    sample = barcodeInfo['no_barcode']['sample']
    reference = barcodeInfo['no_barcode']['referenceName']

    # Scenario 1. No barcodes.
    if len(barcodeInfo)==1:
        datasets["datasets"].append({
            "dataset_name"      : sample,
            "file_prefix"       : "rawlib",
            "read_groups"       : [runID,]
        })
        datasets["read_groups"][runID] = {
            "index"             : 0,
            "sample"            : sample,
            #"library"           : library,
            "reference"         : reference,
            "description"       : ''.join(ch for ch in notes if ch.isalnum() or ch == " "),
            "platform_unit"     :  "%s/%s" % (platform,chipType.replace('"',""))
        }

    # Scenario 2. Barcodes present
    else:
        datasets["barcode_config"] = {}
        datasets["datasets"].append({
            "dataset_name"      : sample + "/No_barcode_match",
            "file_prefix"       : "nomatch_rawlib",
            "read_groups"       : [runID+".nomatch",]
        })

        datasets["read_groups"][runID+".nomatch"] = {
            "index"             : 0,
            "sample"            : sample,
            #"library"           : library,
            #"reference"         : reference,
            "reference"         : "",
            "description"       : ''.join(ch for ch in notes if ch.isalnum() or ch == " "),
            "platform_unit"     :  "%s/%s/%s" % (platform,chipType.replace('"',""),"nomatch")
        }
        datasets["barcode_config"]["barcode_id"] = barcodeId

        try:
            for barcode_name,barcode_info in sorted(barcodeInfo.iteritems()):

                if barcode_name == 'no_barcode':
                    continue

                # get per-barcode sample names and reference
                bcsample = barcode_info['sample']
                bcreference = barcode_info['referenceName']

                datasets["datasets"].append({
                    "dataset_name"      : bcsample + "/" + barcode_name,
                    "file_prefix"       : '%s_rawlib' % barcode_name,
                    "read_groups"       : [runID+"."+barcode_name,]
                })

                datasets["read_groups"][runID+"."+barcode_name] = {
                    "barcode_name"      : barcode_name,
                    "barcode_sequence"  : barcode_info['sequence'],
                    "barcode_adapter"   : barcode_info['adapter'],
                    "index"             : barcode_info['index'],
                    "sample"            : bcsample,
                    #"library"           : library,
                    "reference"         : bcreference,
                    "description"       : ''.join(ch for ch in notes if ch.isalnum() or ch == " "),
                    "platform_unit"     :  "%s/%s/%s" % (platform,chipType.replace('"',""),barcode_name)
                }
        
        except:
            print traceback.format_exc()
            datasets["read_groups"] = {}
            datasets["datasets"] = []

    f = open(datasets_json_path,"w")
    json.dump(datasets, f, indent=4)
    f.close()
    

