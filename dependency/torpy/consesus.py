# Copyright 2019 James Brown
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import re
import socket
import random
import logging
import functools
import urllib
from base64 import b32decode, b16encode
from datetime import datetime, timedelta
from enum import auto, Flag
from threading import Lock
from typing import List

from dependency.torpy.utils import retry, log_retry
from dependency.torpy.documents import TorDocumentsFactory
from dependency.torpy.guard import TorGuard
from dependency.torpy.parsers import RouterDescriptorParser
from dependency.torpy.cache_storage import TorCacheDirStorage
from dependency.torpy.crypto_common import rsa_verify, rsa_load_der
from dependency.torpy.documents.network_status import RouterFlags, NetworkStatusDocument, FetchDescriptorError, Router
from dependency.torpy.documents.dir_key_certificate import DirKeyCertificateList
from dependency.torpy.documents.network_status_diff import NetworkStatusDiffDocument
from dependency.torpy.dirs import AUTHORITY_DIRS, FALLBACK_DIRS

# from stem.control import Controller

logger = logging.getLogger(__name__)


class DirectoryFlags(Flag):
    # tor ref: dirinfo_type_t

    NO_DIRINFO = 0
    # /** Serves/signs v3 directory information: votes, consensuses, certs */
    V3_DIRINFO = auto()
    # /** Serves bridge descriptors. */
    BRIDGE_DIRINFO = auto()
    # /** Serves extrainfo documents. */
    EXTRAINFO_DIRINFO = auto()
    # /** Serves microdescriptors. */
    MICRODESC_DIRINFO = auto()

    @classmethod
    def ALL(cls):  # noqa: N802
        retval = cls.NO_DIRINFO
        for member in cls.__members__.values():
            retval |= member
        return retval


class DirectoryServer(Router):
    """This class represents a ,;,. ,. directory authority."""

    HEX_DIGEST_LEN = 40
    AUTH_FLAGS = DirectoryFlags.V3_DIRINFO | DirectoryFlags.EXTRAINFO_DIRINFO | DirectoryFlags.MICRODESC_DIRINFO

    def __init__(self, nickname, ip, dir_port, or_port, fingerprint, v3ident=None, ipv6=None, bridge=False,
                 dir_flags=DirectoryFlags.NO_DIRINFO, flags=None):
        flags = [RouterFlags.Authority] if flags is None else flags
        super().__init__(nickname, bytes.fromhex(fingerprint),
                         ip, int(or_port), int(dir_port), flags)
        self._dir_flags = dir_flags

        if v3ident:
            assert len(v3ident) == DirectoryServer.HEX_DIGEST_LEN, f'Bad v3 identity digest "{v3ident}"'
            # tor ref: parse_dir_authority_line
            self._dir_flags = DirectoryServer.AUTH_FLAGS
        self._v3ident = v3ident
        self._ipv6 = ipv6

        self._bridge = bool(bridge)
        if self._bridge:
            self._dir_flags |= DirectoryFlags.BRIDGE_DIRINFO

    @classmethod
    def from_fallback_str(cls, string):
        # tor ref: parse_dir_fallback_line

        # "185.225.17.3:80 orport=443 id=0338F9F55111FE8E3570E7DE117EF3AF999CC1D7 ipv6=[2a0a:c800:1:5::3]:443"
        m = re.match(
            '\"(?P<ip>[0-9\\.]*):(?P<dir_port>[0-9]{1,5}) orport=(?P<or_port>[0-9]{1,5}) '
            'id=(?P<fingerprint>[A-F0-9]*)( ipv6=(?P<ipv6>.*))?\"', string)
        if not m:
            raise Exception("Can't parse fallback dir line: " + string)

        return cls(None, **m.groupdict(), dir_flags=DirectoryFlags.ALL(), flags=[])

    @classmethod
    def from_authority_str(cls, string):
        # tor ref: parse_dir_authority_line
        m = re.match(
            '\"(?P<nickname>.+?) orport=(?P<or_port>[0-9]{2,5})( (?P<bridge>bridge))?'
            '( v3ident=(?P<v3ident>[A-F0-9]+))?( ipv6=(?P<ipv6>[0-9a-f:\\[\\]]+))? '
            '(?P<ip>[0-9\\.]+):(?P<dir_port>[0-9]{2,5}) (?P<fingerprint>[0-9A-F\\s]+)\"', string)

        if not m:
            raise Exception("Can't parse fallback dir line: " + string)

        return cls(**m.groupdict())

    @property
    def v3ident(self):
        return self._v3ident

    @property
    def dir_flags(self):
        return self._dir_flags


class DirectoryList:
    def __init__(self, lst):
        self._directories: List[DirectoryServer] = lst

    @classmethod
    def default_authorities(cls, use_local_directories=True):
        if use_local_directories:
            # controller = Controller.from_port(port=8000)
            # controller.authenticate("password")
            # # nodes_list = [
            # #     "\"%s orport=%s v3ident=%s %s:%s %s\"" % (
            # #         desc.nickname, desc.or_port, desc.digest,
            # #         desc.address, desc.dir_port, ' '.join((lambda s:[s[i:i+4] for i in range(0, len(s), 4)])(desc.fingerprint))
            # #     ) for desc in controller.get_network_statuses()
            # #     if 'a' in desc.nickname
            # # ]
            # full_consensus = urllib.request.urlopen("http://127.0.0.1:7000/tor/status-vote/current/consensus").read().decode()
            # v3ident = list(reversed(list(map(
            #     lambda s : s.split(' ')[-2],
            #     filter(
            #         lambda line : 'directory-signature' in line,
            #         full_consensus.split('\n')
            #     )
            # ))))
            # fingerprint = list(reversed(list(map(
            #     lambda s : s.split(' ')[-1],
            #     filter(
            #         lambda line : 'directory-signature' in line,
            #         full_consensus.split('\n')
            #     )
            # ))))
            # nodes_list = [
            #     "\"%s orport=%s v3ident=%s %s:%s %s\"" % (
            #         desc.nickname, desc.or_port, v3ident.pop(),#desc.digest,
            #         desc.address, desc.dir_port, ' '.join((lambda s:[s[i:i+4] for i in range(0, len(s), 4)])(fingerprint.pop()))
            #     ) for desc in controller.get_network_statuses()
            #     if 'a' in desc.nickname
            # ]
            # logger.debug("List of relays found on the network:", '\n'.join(nodes_list))

            # Hard-code this one just for speed, becasue the docker is always running the same configuration
            nodes_list = [
                "\"test000relay orport=5000 v3ident=F76CB79A2D20FF3F5B8004C5F9CFD44E45FAE65A 127.0.0.1:7000 5DE4 2B9E FE40 67AC 49C6 6153 6913 D223 847B 4F5B\"",
                "\"test001relay orport=5001 v3ident=6888B7C7AF3B28B84BD9941D628D8A60BDB71561 127.0.0.1:7001 E38E BE07 57A6 C36B BCC7 45F7 9901 987F A176 AE6B\"",
                "\"test002relay orport=5002 v3ident=008FDAE64EC6EB0D64E83BD0AB790342CC9C557E 127.0.0.1:7002 E58A 9B7F D9C6 B66C 0C07 B145 EF20 4F5B C1F1 2A08\""
            ]
        else:
            data = AUTHORITY_DIRS.replace('  ', ' ').replace('\n', '').replace('\" \"', '')
            nodes_list = data.rstrip(',').split(',')
        return cls(list(map(DirectoryServer.from_authority_str, nodes_list)))

    @classmethod
    def default_fallbacks(cls):
        def remove_comments(string):
            string = re.sub(re.compile('/\\*.*?\\*/', re.DOTALL), '', string)
            string = re.sub(re.compile('//.*?\\n'), '', string)
            return string

        data = remove_comments(FALLBACK_DIRS)
        data = data.replace('\n', '').replace('\"\"', '')
        nodes_list_str = data.rstrip(',').split(',')
        return cls(list(map(DirectoryServer.from_fallback_str, nodes_list_str)))

    def find(self, identity):
        return next((directory for directory in self._directories if directory.v3ident == identity), None)

    def filter(self, dir_flags):
        return filter(lambda directory: directory.dir_flags & dir_flags, self._directories)

    def count(self, dir_flags):
        return sum(1 for _ in self.filter(dir_flags))

    @property
    def total(self):
        return len(self._directories)

    def get_random(self):
        return random.choice(self._directories)


class Descriptor:
    def __init__(self, onion_key, signing_key, ntor_key):
        self._onion_key = onion_key
        self._signing_key = signing_key
        self._ntor_key = ntor_key

    @property
    def onion_key(self):
        return self._onion_key

    @property
    def signing_key(self):
        return self._signing_key

    @property
    def ntor_key(self):
        return self._ntor_key


def expire_dir_guard_on_error():
    def decorator(func):
        def newfn(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                if args[0]._dir_guard:
                    args[0]._dir_guard_ttl = datetime.utcnow()
                raise

        return newfn

    return decorator


class TorConsensus:
    def __init__(self, authorities=None, fallbacks=None, cache_storage=None, use_local_directories=True):
        self._lock = Lock()

        self._authorities = authorities or DirectoryList.default_authorities(use_local_directories)
        logger.debug('Loaded %i authorities dir', self._authorities.total)

        self._fallbacks = fallbacks or DirectoryList.default_authorities(use_local_directories)
        logger.debug('Loaded %i fallbacks dir', self._fallbacks.total)

        self._cache_storage = cache_storage or TorCacheDirStorage()
        self._document = self._cache_storage.load_document(NetworkStatusDocument)
        if self._document:
            self._document.link_consensus(self)
            logger.debug('Loaded %i consensus routers', len(self._document.routers))
        self._certs = self._cache_storage.load_document(DirKeyCertificateList)

        self._dir_guard_ttl = self._dir_guard = self._dir_circuit = None

    @property
    def document(self):
        return self.get_document()

    def get_document(self, with_renew=True):
        if with_renew:
            self.renew()
        return self._document

    def close(self):
        if self._dir_guard:
            self._dir_guard.close()

    @retry(5, BaseException, delay=1, backoff=2,
           log_func=functools.partial(log_retry, msg='Retry with another router...', no_traceback=(socket.timeout,)))
    def renew(self, force=False):
        with self._lock:
            if not force and self._document and self._document.is_live:
                return

            # tor ref: networkstatus_set_current_consensus
            prev_hash = self._document.digest_sha3_256.hex() if self._document else None
            raw_string = self.download_consensus(prev_hash)

            # Make sure it's parseable
            new_doc = TorDocumentsFactory.parse(raw_string, possible=(NetworkStatusDocument, NetworkStatusDiffDocument))
            if new_doc is None:
                return
                raise Exception('Unknown document has been received: ' + raw_string)

            if type(new_doc) is NetworkStatusDiffDocument:
                new_doc = self._document.apply_diff(new_doc)

            new_doc.link_consensus(self)

            verified, signing_idents = self.verify(new_doc)
            if not verified:
                self.renew_certs(signing_idents)

                # Try verify again
                verified, _ = self.verify(new_doc)
                if not verified:
                    raise Exception('Invalid consensus')

            # Use new consensus document
            self._document = new_doc
            self._cache_storage.save_document(new_doc)

    def verify(self, new_doc):
        # tor ref: networkstatus_check_consensus_signature
        signed = 0
        # more 50% percents of V3_DIRINFO authorities sign
        total_signers = self._authorities.count(DirectoryFlags.V3_DIRINFO)
        required = int(total_signers / 2)

        signing_idents = []
        for voter in new_doc.voters:
            sign = new_doc.find_signature(voter.fingerprint)
            if not sign:
                logger.debug('Not sign by %s (%s)', voter.nickname, voter.fingerprint)
                continue

            trusted = self._authorities.find(sign['identity'])
            if not trusted:
                logger.warning('Unknown voter present')
                continue

            doc_digest = new_doc.get_digest(sign['algorithm'])

            pubkey = self._get_pubkey(sign['identity'])
            if pubkey and rsa_verify(pubkey, sign['signature'], doc_digest):
                signed += 1

            signing_idents.append((sign['identity'], sign['signing_key_digest']))

        return signed > required, signing_idents

    def _get_pubkey(self, identity):
        if self._certs:
            cert = self._certs.find(identity)
            if cert:
                return rsa_load_der(cert.dir_signing_key)

    @retry(3, BaseException,
           log_func=functools.partial(log_retry, msg='Retry with another router...'))
    def renew_certs(self, signing_idents):
        key_certificates_raw = self.download_public_keys(signing_idents)
        certs = DirKeyCertificateList(key_certificates_raw)
        self._certs = certs
        self._cache_storage.save_document(certs)

    def get_router(self, fingerprint) -> Router:
        # TODO: make mapping with fingerprint as key?
        fingerprint_b = b32decode(fingerprint.upper())
        return next(onion_router for onion_router in self.document.routers if onion_router.fingerprint == fingerprint_b)

    def get_routers(self, flags=None, exclude_flags=None, has_dir_port=True, with_renew=True):
        """
        Select consensus routers that satisfy certain parameters.

        :param flags: Router flags
        :param has_dir_port: Has dir port
        :param with_renew: do renew consensus if old
        :return: return list of routers
        """
        results = []

        for onion_router in self.get_document(with_renew=with_renew).routers:
            if flags and not all(f in onion_router.flags for f in flags):
                continue
            if exclude_flags and any(f in onion_router.flags for f in exclude_flags):
                continue
            if has_dir_port and not onion_router.dir_port:
                continue
            results.append(onion_router)

        return results

    def get_random_router(self, flags=None, has_dir_port=None, with_renew=True):
        """
        Select a random consensus router that satisfy certain parameters.

        :param flags: Router flags
        :param has_dir_port: Has dir port
        :param with_renew: Do renew consensus if old
        :return: router
        """
        routers = self.get_routers(flags, has_dir_port, with_renew)
        return random.choice(routers)

    def get_random_guard_node(self, different_flags=None):
        flags = different_flags or [RouterFlags.Guard]
        router = self.get_random_router(flags)
        logger.info(' | get_random_guard_node | ' + router.ip + ':' + router.dir_port + ' AKA ' + router.nickname)
        return router

    def get_random_exit_node(self):
        flags = [RouterFlags.Fast, RouterFlags.Running, RouterFlags.Valid, RouterFlags.Exit]
        router = self.get_random_router(flags)
        logger.info(' | get_random_exit_node | ' + router.ip + ':' + router.dir_port + ' AKA ' + router.nickname)
        return router

    def get_random_middle_node(self):
        flags = [RouterFlags.Fast, RouterFlags.Running, RouterFlags.Valid]
        router = self.get_random_router(flags)
        logger.info(' | get_random_middle_node | ' + router.ip + ':' + router.dir_port + ' AKA ' + router.nickname)
        return router

    def get_hsdirs(self):
        flags = [RouterFlags.HSDir]
        print('get_hsdirs')
        return self.get_routers(flags, has_dir_port=True)

    def _create_dir_circuit(self, purpose=None):
        if self._document and self._document.is_reasonably_live:
            router = self.get_random_router(flags=[RouterFlags.Guard], with_renew=False)
        else:
            logger.debug('There is no reasonable live consensus... use fallback dirs')
            router = self._fallbacks.get_random()

        # tor ref: directory_get_from_dirserver DIR_PURPOSE_FETCH_CONSENSUS
        # tor ref: directory_send_command
        guard = TorGuard(router, purpose=purpose)
        return guard, guard.create_circuit(0)

    def _get_dir_client(self):
        if self._dir_guard_ttl and self._dir_guard_ttl < datetime.utcnow():
            self._dir_guard.close()
            self._dir_guard_ttl = self._dir_guard = self._dir_circuit = None

        if not self._dir_circuit:
            logger.debug('There is no internal dir circuit yet. Creating it...')
            self._dir_guard, self._dir_circuit = self._create_dir_circuit(purpose='Internal dir client')
            self._dir_guard_ttl = datetime.utcnow() + timedelta(hours=1)

        return self._dir_circuit.create_dir_client()

    @property
    def consensus_url(self):
        # tor ref: directory_get_consensus_url
        fpr_list_str = '+'.join([router.v3ident[:6] for router in self._authorities.filter(DirectoryFlags.V3_DIRINFO)])
        return f'/tor/status-vote/current/consensus/{fpr_list_str}.z'

    @expire_dir_guard_on_error()
    def download_consensus(self, prev_hash=None):
        logger.info('Downloading new consensus...')
        headers = {'X-Or-Diff-From-Consensus': prev_hash} if prev_hash else None
        with self._get_dir_client() as dir_client:
            logger.info('\t\t'+self.consensus_url)
            _, body = dir_client.get(self.consensus_url, headers=headers)
            return body.decode()

    @property
    def fp_sk_url(self):
        return '/tor/keys/fp-sk'

    @expire_dir_guard_on_error()
    def download_public_keys(self, signing_idents):
        logger.info('Downloading public keys...')

        fp_sks = '+'.join([f'{identity}-{keyid}' for (identity, keyid) in signing_idents])
        url = f'{self.fp_sk_url}/{fp_sks}.z'

        with self._get_dir_client() as dir_client:
            _, body = dir_client.get(url)
            return body.decode()

    @staticmethod
    def _descriptor_url(fingerprint):
        return f'/tor/server/fp/{b16encode(fingerprint).decode()}'

    @retry(5, BaseException,
           log_func=functools.partial(log_retry, msg='Retry with another router...',
                                      no_traceback=(FetchDescriptorError,)))
    @expire_dir_guard_on_error()
    def get_descriptor(self, fingerprint):
        """
        Get router descriptor by its fingerprint through randomly selected router.

        :param fingerprint:
        :return:
        """
        url = self._descriptor_url(fingerprint)
        try:
            with self._get_dir_client() as dir_client:
                status, response = dir_client.get(url)
            if status != 200:
                raise FetchDescriptorError(f"Can't fetch descriptor from {url}. Status = {status}")
            logger.info('Got descriptor')
        except TimeoutError as e:
            logger.debug(e)
            raise FetchDescriptorError(f"Can't fetch descriptor from {url}")

        descriptor_info = RouterDescriptorParser.parse(response.decode())
        return Descriptor(**descriptor_info)

    def get_responsibles(self, hidden_service):
        """
        Get responsible dir for hidden service specified.

        :param hidden_service:
        :return:
        """
        hsdir_router_list = self.get_hsdirs()

        # Search for the 2 sets of 3 hidden service directories.
        for replica in range(2):
            descriptor_id = hidden_service.get_descriptor_id(replica)
            for i, dir in enumerate(hsdir_router_list):
                if dir.fingerprint > descriptor_id:
                    for j in range(3):
                        idx = (i + 1 + j) % len(hsdir_router_list)
                        yield hsdir_router_list[idx]
                    break
