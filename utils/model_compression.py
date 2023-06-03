#!/usr/bin/env python

import h5py
import numpy as np
import struct

import sys
import argparse
import json
import itertools

class CompressActor(object):
    RECORD_FMT = '>f'

    def __init__(self, fname, ofile, weight_file):
        self.fname = fname
        self.ofile = ofile
        self.weight_file = weight_file

    def act(self):
        raise NotImplementedError()

class Compressor(CompressActor):
    def act(self):
        self.out_floats = []
        out_struct = {
            'groups' : []
        }
        with h5py.File(self.fname, 'r') as dataset:
            for key in dataset.keys():
                subgroup = dataset[key]
                outgroup = {
                    'datasets' : []
                }
                assert type(subgroup) == h5py.Group
                for gkey in subgroup.keys():
                    datablock = subgroup[gkey]
                    assert type(datablock) == h5py.Dataset
                    outgroup['datasets'].append(
                        [gkey, self.output_datablock(datablock)])
                outgroup['attr'] = list(map(lambda t: (t[0], int(t[1])),
                                            subgroup.attrs.items()))
                out_struct['groups'].append([key, outgroup])
            out_struct['attr'] = list(map(lambda t: (t[0], int(t[1])),
                                          dataset.attrs.items()))
        self.output_head(out_struct)

    def output_datablock(self, datablock):
        self.out_floats += datablock[:].flatten().tolist()
        return list(datablock.shape)

    def write_weight(self, weight, ofile):
        ofile.write(struct.pack(self.RECORD_FMT, weight))

    def output_head(self, out_struct):
        with open(self.ofile, 'w') as ofile:
            json.dump(out_struct, ofile)
        with open(self.weight_file, 'wb') as ofile:
            for item in self.out_floats:
                self.write_weight(item, ofile)

class Decompressor(CompressActor):
    def act(self):
        with open(self.fname, 'r') as ifile:
            item = json.load(ifile)
        with open(self.weight_file, 'rb') as weightfile:
            chunksize = struct.calcsize(self.RECORD_FMT)
            self.weights = []
            chunk = weightfile.read(chunksize)
            while chunk != b'':
                self.weights.append(self.read_weight(chunk))
                chunk = weightfile.read(chunksize)
        self.output(item)

    def read_weight(self, chunk):
        return struct.unpack(self.RECORD_FMT, chunk)[0]

    def calc_num_elems(self, dimensions):
        num_elems = 1
        for dimension in dimensions:
            num_elems *= dimension
        return num_elems

    def output(self, item):
        with h5py.File(self.ofile, 'w') as ofile:
            ctr = 0
            for agroup in item['groups']:
                key, groups = agroup
                grp = ofile.create_group(key)
                for attr in groups['attr']:
                    grp.attrs[attr[0]] = attr[1]
                for adataset in groups['datasets']:
                    name, shape = adataset
                    num_elems = self.calc_num_elems(shape)
                    data = np.reshape(self.weights[ctr:num_elems + ctr], shape)
                    grp.create_dataset(name, data=data, dtype=np.float32)
                    ctr += num_elems
            for attr in item['attr']:
                ofile.attrs[attr[0]] = attr[1]

def main(args):
    assert args.compress or args.decompress, (
        'Must provide compress or decompress argument')
    (Compressor if args.compress else Decompressor)(
        args.ifile, args.ofile, args.weight_file).act()

if __name__=='__main__':
    parser = argparse.ArgumentParser(
        description='Compress and decompress model. ')
    parser.add_argument('ifile', help='Input file. ')
    parser.add_argument('ofile', help='Output file. ')
    parser.add_argument('weight_file', help='File for weights. ')
    parser.add_argument('-c', '--compress', action='store_true')
    parser.add_argument('-d', '--decompress', action='store_true')
    main(parser.parse_args())
