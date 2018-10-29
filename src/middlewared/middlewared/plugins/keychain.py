import base64
import errno
import os
import random
import re
import string
import subprocess
import tempfile
import urllib.parse

from middlewared.client import Client
from middlewared.service_exception import CallError
from middlewared.schema import (Bool, Dict, File, Int, Patch, Str,
                                ValidationErrors, accepts, validate_attributes)
from middlewared.service import CRUDService, private
from middlewared.utils import run


class KeychainCredentialType:
    name = NotImplemented
    title = NotImplemented

    credentials_schema = NotImplemented

    async def validate_and_pre_save(self, middleware, verrors, schema_name, attributes):
        pass


class SSHKeyPair(KeychainCredentialType):
    name = "SSH_KEY_PAIR"
    title = "SSH Key Pair"

    credentials_schema = [
        Str("private_key", null=True, default=None),
        Str("public_key", null=True, default=None),
    ]

    async def validate_and_pre_save(self, middleware, verrors, schema_name, attributes):
        if attributes["private_key"]:
            with tempfile.NamedTemporaryFile("w+") as f:
                os.chmod(f.name, 0o600)

                f.write(attributes["private_key"])
                f.flush()

                proc = await run(["ssh-keygen", "-y", "-f", f.name], check=False, encoding="utf8")
                if proc.returncode == 0:
                    public_key = proc.stdout
                else:
                    verrors.add(f"{schema_name}.private_key", proc.stderr)
                    return

            if attributes["public_key"]:
                if " ".join(attributes["public_key"].split()[:2]) != public_key:
                    verrors.add(f"{schema_name}.public_key", "Private key and public key do not match")
            else:
                attributes["public_key"] = public_key

        if not attributes["public_key"]:
            verrors.add(f"{schema_name}.public_key", "You must specify at least public key")
            return

        with tempfile.NamedTemporaryFile("w+") as f:
            os.chmod(f.name, 0o600)

            f.write(attributes["public_key"])
            f.flush()

            proc = await run(["ssh-keygen", "-l", "-f", f.name], check=False, encoding="utf8")
            if proc.returncode != 0:
                verrors.add(f"{schema_name}.public_key", "Invalid public key")
                return


class SSHCredentials(KeychainCredentialType):
    name = "SSH_CREDENTIALS"
    title = "SSH credentials"

    credentials_schema = [
        Str("host", required=True),
        Str("port", default=22),
        Str("username", default="root"),
        Int("private_key", required=True),
        Str("remote_host_key", required=True),
        Str("cipher", enum=["STANDARD", "FAST", "DISABLED"], default="STANDARD"),
        Int("connect_timeout", default=10),
    ]


TYPES = {
    type.name: type()
    for type in [SSHKeyPair, SSHCredentials]
}


async def get_ssh_key_pair_with_private_key(middleware, id):
    try:
        credential = await middleware.call("keychaincredential.query", [["id", "=", id]], {"get": True})
    except IndexError:
        return None

    if credential["type"] != "SSH_KEY_PAIR":
        return None

    if not credential["attributes"]["private_key"]:
        return None

    return credential


def process_ssh_keyscan_output(output):
    return [" ".join(line.split()[1:]) for line in output.split("\n") if line and not line.startswith("# ")][-1]


class KeychainCredentialService(CRUDService):

    class Config:
        datastore = "system.keychaincredential"

    @accepts(Dict(
        "keychain_credential_create",
        Str("name", required=True),
        Str("type", required=True),
        Dict("attributes", additional_attrs=True, required=True),
        register=True,
    ))
    async def do_create(self, data):
        await self._validate("keychain_credential_create", data)

        data["id"] = await self.middleware.call(
            "datastore.insert",
            self._config.datastore,
            data,
        )
        return data

    @accepts(
        Int("id"),
        Patch(
            "keychain_credential_create",
            "keychain_credential_update",
            ("attr", {"update": True})
        )
    )
    async def do_update(self, id, data):
        old = await self._get_instance(id)

        new = old.copy()
        new.update(data)

        await self._validate("keychain_credentials_update", new, id)

        await self.middleware.call(
            "datastore.update",
            self._config.datastore,
            id,
            new,
        )

        data["id"] = id

        return data

    @accepts(Int("id"))
    async def do_delete(self, id):
        await self.middleware.call(
            "datastore.delete",
            self._config.datastore,
            id,
        )

    async def _validate(self, schema_name, data, id=None):
        verrors = ValidationErrors()

        await self._ensure_unique(verrors, schema_name, "name", data["name"], id)

        if data["type"] not in TYPES:
            verrors.add(f"{schema_name}.type", "Invalid type")
            raise verrors
        else:
            type = TYPES[data["type"]]

            attributes_verrors = validate_attributes(type.credentials_schema, data)
            verrors.add_child(f"{schema_name}.attributes", attributes_verrors)

        if verrors:
            raise verrors

        await type.validate_and_pre_save(self.middleware, verrors, f"{schema_name}.attributes", data["attributes"])

    @accepts()
    def generate_ssh_key_pair(self):
        key = os.path.join("/tmp", "".join(random.choice(string.ascii_letters) for _ in range(32)))
        if os.path.exists(key):
            os.unlink(key)
        if os.path.exists(f"{key}.pub"):
            os.unlink(f"{key}.pub")
        try:
            subprocess.check_call(["ssh-keygen", "-t", "rsa", "-f", key, "-N", "", "-q"])
            with open(key) as f:
                private_key = f.read()
            with open(f"{key}.pub") as f:
                public_key = f.read()
        finally:
            if os.path.exists(key):
                os.unlink(key)
            if os.path.exists(f"{key}.pub"):
                os.unlink(f"{key}.pub")

        return {
            "private_key": private_key,
            "public_key": public_key,
        }

    @accepts(Dict(
        "keychain_remote_ssh_host_key_scan",
        Str("host", required=True),
        Str("port", default=22),
        Int("connect_timeout", default=10),
    ))
    async def remote_ssh_host_key_scan(self, data):
        proc = await run(["ssh-keyscan", "-p", str(data["port"]), "-T", str(data["connect_timeout"]), data["host"]],
                         check=False, encoding="utf8")
        if proc.returncode == 0:
            return process_ssh_keyscan_output(proc.stdout)
        else:
            raise CallError(proc.stderr)

    @accepts(Dict(
        "keychain_remote_ssh_semiautomatic_setup",
        Str("name", required=True),
        Str("url", required=True),
        Str("token", required=True),
        Str("username", default="root"),
        Int("private_key", required=True),
        Str("cipher", enum=["STANDARD", "FAST", "DISABLED"], default="STANDARD"),
        Int("connect_timeout", default=10),
    ))
    def remote_ssh_semiautomatic_setup(self, data):
        replication_key = self.middleware.run_coroutine(
            get_ssh_key_pair_with_private_key(self.middleware, data["private_key"]))

        with Client(os.path.join(re.sub("^http", "ws", data["url"]), "websocket")) as c:
            if not c.call("auth.token", data["token"]):
                raise CallError("Invalid token")

            try:
                response = c.call("replication.pair", {
                    "hostname": "any-host",
                    "public-key": replication_key["attributes"]["public_key"],
                    "user": data["username"],
                })
            except Exception as e:
                raise CallError(f"Semi-automatic SSH connection setup failed: {e!r}")

        return self.middleware.call_sync("keychaincredential.create", {
            "name": data["name"],
            "type": "SSH_CREDENTIALS",
            "attributes": {
                "host": urllib.parse.urlparse(data["url"]).hostname,
                "port": response["ssh_port"],
                "username": data["username"],
                "private_key": replication_key["id"],
                "remote_host_key": process_ssh_keyscan_output(response["ssh_hostkey"]),
                "cipher": data["cipher"],
                "connect_timeout": data["connect_timeout"],
            }
        })

    @private
    @accepts(Dict(
        "keychain_ssh_pair",
        Str("remote_hostname", required=True),
        Str("username", default="root"),
        Str("public_key", required=True),
    ))
    async def ssh_pair(self, data):
        """
        Receives public key, storing it to accept SSH connection and return
        pertinent SSH data of this machine.
        """
        service = await self.middleware.call(
            "datastore.query", "services.services", [("srv_service", "=", "ssh")], {"get": True})
        ssh = await self.middleware.call("datastore.query", "services.ssh", None, {"get": True})
        try:
            user = await self.middleware.call(
                "datastore.query", "account.bsdusers", [("bsdusr_username", "=", data["username"])],
                {"get": True})
        except IndexError:
            raise CallError(f"User {data['username']} does not exist")

        if user["bsdusr_home"].startswith("/nonexistent") or not os.path.exists(user["bsdusr_home"]):
            raise CallError(f"Home directory {user['bsdusr_home']} does not exist", errno.ENOENT)

        # Make sure SSH is enabled
        if not service["srv_enable"]:
            await self.middleware.call("datastore.update", "services.services", service["id"], {"srv_enable": True})
            await self.middleware.call("notifier.start", "ssh")

            # This might be the first time of the service being enabled
            # which will then result in new host keys we need to grab
            ssh = await self.middleware.call("datastore.query", "services.ssh", None, {"get": True})

        # If .ssh dir does not exist, create it
        dotsshdir = os.path.join(user["bsdusr_home"], ".ssh")
        if not os.path.exists(dotsshdir):
            os.mkdir(dotsshdir)
            os.chown(dotsshdir, user["bsdusr_uid"], user["bsdusr_group"]["bsdgrp_gid"])

        # Write public key in user authorized_keys for SSH
        authorized_keys_file = f"{dotsshdir}/authorized_keys"
        with open(authorized_keys_file, "a+") as f:
            f.seek(0)
            if data["public-key"] not in f.read():
                f.write("\n" + data["public-key"])

        ssh_hostkey = "{0} {1}\n{0} {2}\n{0} {3}\n".format(
            data["remote_hostname"],
            base64.b64decode(ssh["ssh_host_rsa_key_pub"].encode()).decode(),
            base64.b64decode(ssh["ssh_host_ecdsa_key_pub"].encode()).decode(),
            base64.b64decode(ssh["ssh_host_ed25519_key_pub"].encode()).decode(),
        )

        return {
            "port": ssh["ssh_tcpport"],
            "host_key": ssh_hostkey,
        }