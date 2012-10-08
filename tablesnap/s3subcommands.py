"""Sub Commands that use S3 
"""
import copy
import errno
import json
import logging
import os
import pwd
import grp
import socket

import boto, boto.utils

import file_util, snapsubcommands

class S3SnapConfig(object):
    """S3 config. 
    """

    def __init__(self, bucket_name, aws_key, aws_secret, prefix, 
        max_file_size_mb, chunk_size_mb,
        retries):

        self.bucket_name = bucket_name
        self.aws_key = aws_key
        self.aws_secret = aws_secret
        self.bucket_name = bucket_name
        self.prefix = prefix or ""
        self.max_file_size_mb = max_file_size_mb 
        self.chunk_size_mb = chunk_size_mb
        self.retries = retries

    @classmethod    
    def from_args(cls, args):
        return S3SnapConfig(args.bucket_name, args.aws_key, args.aws_secret, 
            args.prefix, 
            args.max_upload_size_mb, args.multipart_chunk_size_mb, 
            args.retries)

    @property
    def max_file_size_bytes(self):
        return self.max_file_size_mb * 2**20

    @property
    def chunk_size_bytes(self):
        return self.chunk_size_mb * 2 ** 20

class S3SnapSubCommand(snapsubcommands.SnapSubCommand):
    """SubCommand to store a file in S3. 
    """

    log = logging.getLogger("%s.%s" % (__name__, "S3SnapSubCommand"))

    # command description used by the base 
    command_name = "snap-s3"
    command_help = "Upload new SSTables to Amazon S3"
    command_description = "Upload new SSTables to Amazon S3"


    @classmethod
    def add_sub_parser(cls, sub_parsers):
        """Called to add a parser to ``sub_parsers`` for this command. 
        """
        
        parser = super(S3SnapSubCommand, cls).add_sub_parser(sub_parsers)

        parser.add_argument('-k', '--aws-key', dest='aws_key', default=None,
            help="AWS API Key")
        parser.add_argument('-s', '--aws-secret', dest='aws_secret', 
            default=None, help="AWS API Secret Key")
        parser.add_argument('-p', '--prefix', dest='prefix', default=None,
            help='Set a string prefix for uploaded files in S3')
        parser.add_argument('--max-upload-size-mb', dest='max_upload_size_mb', 
            type=int, default=5120,
            help='Max size for files to be uploaded before doing multipart ')
        parser.add_argument('--multipart-chunk-size-mb', 
            dest='multipart_chunk_size_mb', default=256, type=int,
            help='Chunk size for multipart uploads (10%% of '
            'free memory if default is not available)')
        parser.add_argument('--retries', 
            dest='retries', default=5, type=int,
            help='Number of times to retry s3 calls')


        parser.add_argument('bucket_name',
            help='S3 bucket to upload to.')

        return parser

    def __init__(self, args):
        super(S3SnapSubCommand, self).__init__(args)
        self.s3_config = S3SnapConfig.from_args(args)

    def _create_endpoint(self):
        return S3Endpoint(copy.deepcopy(self.snap_config),
            copy.deepcopy(self.s3_config))

class S3Wrapper(object):
    """Container for S3 functions. 

    TODO: needed ? merge with something else ?
    """

    log = logging.getLogger("%s.%s" % (__name__, "S3Wrapper"))

    def __init__(self, s3_config):
        self.s3_config = s3_config

        self._s3_conn = None
        self._bucket = None

    def _conn(self):
        if self._s3_conn is not None:
            return self._s3_conn

        self._s3_conn = boto.connect_s3(self.s3_config.aws_key, 
            self.s3_config.aws_secret)
        self._s3_conn.retries = self.s3_config.retries

        return self._s3_conn

    def reset(self):

        if self._s3_conn is not None:
            self._s3_conn.close()
        self._s3_conn = None
        self.bucket = None
        return

    def bucket(self):
        
        if self._bucket is not None:
            return self._bucket

        self.log.debug("Getting aws bucket %(bucket_name)s" % \
            vars(self.s3_config))
        self._bucket = self._conn().get_bucket(self.s3_config.bucket_name)
        return self._bucket


    def file_exists(self, key_name, cass_file):
        """
        
        Check if this keyname (ie, file) has already been uploaded to
        the S3 bucket. This will verify that not only does the keyname
        exist, but that the MD5 sum is the same -- this protects against
        partial or corrupt uploads.
        """

        key = self.bucket().get_key(key_name)
        if key == None:
            self.log.debug('Key %(key_name)s does not exist' % vars())
            return False
        
        self.log.debug('Found key %(key_name)s' % vars())

        if key.size != cass_file.file_meta["size"]:
            self.log.warning('ATTENTION: your source (%s) and target (%s) '
                'sizes differ, you should take a look. As immutable files '
                'never change, one must assume the local file got corrupted '
                'and the right version is the one in S3. Will skip this file '
                'to avoid future complications' % (cass_file, key_name, ))
            return True

        key_md5 = key.get_metadata('md5sum')
        if key_md5:
            result = cass_file.file_meta["md5_hex"] == meta
            self.log.debug('MD5 metadata comparison: %s == %s? : %s' %
                          (cass_file.file_meta["md5_hex"], key_md5, result))
        else:
            result = cass_file.file_meta["md5_hex"] == key.etag.strip('"')
            self.log.debug('ETag comparison: %s == %s? : %s' %
                          (cass_file.file_meta["md5_hex"], key.etag.strip('"'),result))
            
            if result:
                self.log.debug('Setting missing md5sum metadata for '
                    ' %(key_name)s' % vars())
                # HACK: bad to write here
                key.set_metadata('md5sum', cass_file.file_meta["md5_hex"])
        
        if result:
            self.log.info("File %(cass_file)s exists at key %(key_name)s"
                % vars())
            return

        self.log.warning('ATTENTION: your source (%s) and target (%s) '
            'MD5 hashes differ, you should take a look. As immutable '
            'files never change, one must assume the local file got '
            'corrupted and the right version is the one in S3. Will '
            'skip this file to avoid future complications' % 
            (cass_file, key_name, ))
        return False


class S3Endpoint(object):
    log = logging.getLogger("%s.%s" % (__name__, "S3Endpoint"))

    def __init__(self, snap_config, s3_config):

        self.snap_config = snap_config
        self.s3_config = s3_config
        self.s3 = S3Wrapper(self.s3_config)


    def store(self, cass_file):
        """Called up upload the ``cass_file``.
        """
        
        file_key_name = self.build_keyname(cass_file)
        if self.s3.file_exists(file_key_name, cass_file):
            self.log.warn("S3 Key %(file_key_name)s for file %(cass_file)s, "\
                "exists skipping" % vars())
            return

        if not self.snap_config.skip_index:
            index_json = json.dumps(file_util._file_index(cass_file.file_path))
            self._do_upload_index(index_json, file_key_name)
        
        is_multipart_upload = cass_file.file_meta["size"] > \
            self.s3_config.max_file_size_bytes
        self.log.debug('File size check: %s > %s ? : %s' %
            (cass_file.file_meta["size"], self.s3_config.max_file_size_bytes,
            is_multipart_upload))

        if is_multipart_upload:
            self.log.info('Performing multipart upload for %s' %
                         (cass_file))
            self._do_multi_part_upload(file_key_name, cass_file)
        else:
            self.log.debug('Performing monolithic upload')
            self._do_single_part_upload(file_key_name, cass_file)
        return

    def build_keyname(self, cass_file):
        

        ep = os.path.join(self.s3_config.prefix, cass_file.backup_path())
        self.log.debug("For file %(cass_file)s aws key is %(ep)s" % vars())
        return ep

    def _do_upload_index(self, index_json, file_key_name):
        """
        """

        if self.snap_config.test_mode:
            self.log.info("TestMode - _do_upload_index %s" % vars())
            return

        index_key = self.s3.bucket().new_key(
                "%(file_key_name)s-listdir.json" % vars())
        index_key.set_contents_from_string(index_json,
            headers={'Content-Type': 'application/json'},
            replace=True)
        
        return

    def _do_multi_part_upload(self, file_key_name, cass_file):

        if self.snap_config.test_mode:
            self.log.info("TestMode - _do_multi_part_upload %s" % vars())
            return

        mp = bucket.initiate_multipart_upload(file_key_name,
            metadata=cass_file.file_meta)
        
        chunk = None
        try:

            for part, chunk in enumerate(self.split_sstable(cass_file.file_path)):
                self.log.debug('Uploading part #%d (size: %d)' %
                           (part, chunk.len,))
                try:
                    mp.upload_part_from_file(chunk, part)
                finally:
                    chunk.close()
        except (Exception):
            mp.cancel_upload()
            raise

        self.log.debug('Uploaded %d parts, completing upload' % (part,))
        mp.complete_upload()
        return

    def _do_single_part_upload(self, file_key_name, cass_file):

        if self.snap_config.test_mode:
            self.log.info("TestMode - _do_single_part_upload %s" % vars())
            return

        self.log.debug('Performing single part upload')

        key = self.s3.bucket().new_key(file_key_name)
        # All meta data fields have to be strings.
        key.update_metadata({
            k : str(v)
            for k, v in cass_file.file_meta.iteritems()
        })
        # Rebuild the MD5 tuple boto makes
        md5 = (
            cass_file.file_meta["md5_hex"], 
            cass_file.file_meta["md5_base64"], 
            cass_file.file_meta["size"]
        )
        key.set_contents_from_filename(cass_file.file_path, replace=True,
            md5=md5)
        return 

    def split_sstable(self, file_path):
        """Yield chunks from ``file_path``.

        """

        free_bytes = self.get_free_memory_in_kb() * 1024
        is_low_memory = free_bytes < self.s3_config.chunk_size_bytes
        self.log.debug('Free memory check: %d < %d ? : %s' %
            (free_bytes, self.s3_config.chunk_size_bytes, is_low_memory))

        if is_low_memory:
            self.log.warn('Your system is low on memory, '
                          'reading in smaller chunks')
            chunk_size = free / 20
        else:
            chunk_size = self.s3_config.chunk_size_bytes
        self.log.debug('Reading %s in %d byte sized chunks' %
                       (file_path, chunk_size))

        with open(file_path, 'rb') as f:
            chunk = f.read(chunk_size)
            while chunk:
                yield StringIO.StringIO(chunk)
                chunk = f.read(chunk_size)                

        return

    def get_free_memory_in_kb(self):

        f = open('/proc/meminfo', 'r')
        memlines = f.readlines()
        f.close()
        lines = []
        for line in memlines:
            ml = line.rstrip(' kB\n').split(':')
            lines.append((ml[0], int(ml[1].strip())))
        d = dict(lines)
        return d['Cached'] + d['MemFree'] + d['Buffers']

