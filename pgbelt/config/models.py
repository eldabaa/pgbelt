from __future__ import annotations

from logging import Logger
from os.path import join
from typing import Any, Callable, Optional  # noqa: F401 # Needed until tiangolo/typer#522 is fixed)
from asyncpg import Pool
from asyncpg import create_pool
from aiofiles import open as aopen
from aiofiles.os import remove
from pgbelt.util import get_logger
from pgbelt.util.asyncfuncs import makedirs
from pgbelt.util.postgres import analyze_table_pkeys
from pydantic import BaseModel
from pydantic import ValidationError
from pydantic import field_validator
from urllib.parse import quote


def config_dir(db: str, dc: str) -> str:
    return f"configs/{dc}/{db}"


def config_file(db: str, dc: str) -> str:
    return join(config_dir(db, dc), "config.json")


def not_empty(v) -> Optional[str]:
    if v == "":
        raise ValueError
    return v


class User(BaseModel):
    """
    Represents a user in a postgres db.

    name: str The user name.
    pw: str The user's password. Only required for users pgbelt needs to log in as.
    """

    name: str
    pw: Optional[str] = None

    _not_empty = field_validator("name", "pw")(not_empty)


class DbConfig(BaseModel):
    """
    Represents a postgres db instance.

    host: str The hostname of this instance.
    ip: str The ip of this instance. Instance IPs must be reachable from one another.
    db: str The dbname to operate on. If you want to migrate multiple dbs in a single instance set up a separate config.
    port: str The port to connect to.
    root_user: User A superuser. Usually the postgres user.
    owner_user: User A user who owns all the data in the your specified schema or who has equivalent permissions. # noqa: RST301
                     This user will end up owning all the data if this is describing the target instance.
    pglogical_user: User A user for use with pglogical. Will be created if it does not exist.
    other_users: list[User] A list of other users whose passwords we might not know.
    """

    host: str
    ip: str
    db: str
    port: str
    root_user: User
    owner_user: User
    pglogical_user: User
    other_users: Optional[list[User]] = None

    _not_empty = field_validator("host", "ip", "db", "port")(not_empty)

    @field_validator("root_user", "owner_user", "pglogical_user")
    def has_password(cls, v) -> User:  # noqa: N805
        if not v.pw:
            raise ValueError
        return v

    @property
    def root_dsn(self) -> str:
        return f"hostaddr={self.ip} port={self.port} dbname={self.db} user={self.root_user.name} password={self.root_user.pw}"

    @property
    def owner_dsn(self) -> str:
        return f"hostaddr={self.ip} port={self.port} dbname={self.db} user={self.owner_user.name} password={self.owner_user.pw}"

    @property
    def pglogical_dsn(self) -> str:
        return f"hostaddr={self.ip} port={self.port} dbname={self.db} user={self.pglogical_user.name} password={self.pglogical_user.pw}"

    @property
    def root_uri(self) -> str:
        password = quote(
            self.root_user.pw
        )  # https://github.com/encode/databases/issues/145#issuecomment-1303792343 need this to handle special characters
        return f"postgresql://{self.root_user.name}:{password}@{self.ip}:{self.port}/{self.db}"

    @property
    def owner_uri(self) -> str:
        password = quote(
            self.owner_user.pw
        )  # https://github.com/encode/databases/issues/145#issuecomment-1303792343 need this to handle special characters
        return f"postgresql://{self.owner_user.name}:{password}@{self.ip}:{self.port}/{self.db}"

    @property
    def pglogical_uri(self) -> str:
        password = quote(
            self.pglogical_user.pw
        )  # https://github.com/encode/databases/issues/145#issuecomment-1303792343 need this to handle special characters
        return f"postgresql://{self.pglogical_user.name}:{password}@{self.ip}:{self.port}/{self.db}"


class FilterConfig(BaseModel):
    """
    Represents a object that includes the include and the exclude lists.

    include: list of string.
    exclude: list of string.
    """
    
    include: Optional[list[str]] = None
    exclude: Optional[list[str]] = None
    
    @classmethod
    async def apply(self, list_of_items: list[str]) -> list[str]:
        filtered = []
        if self.include is not None:
            filtered = [item for item in list_of_items if item in self.include]
        
        # Filter based on the exclude list if provided
        if self.exclude is not None:
            filtered = [item for item in list_of_items if item not in self.exclude]

        return filtered

class DbupgradeConfig(BaseModel):
    """
    Represents a migration to be performed.

    db: str A name used to identify this specific database pair. Used in cli commands.
    dc: str A name used to identify the environment this database pair is in. Used in cli commands.
    src: DbConfig The database we are moving data out of.
    dst: DbConfig The database we are moving data into.
    tables: Optional[FilterConfig] A object for filters to filter out tables ending up with a list of tables to replicate. If not provided all tables in the named schema will be replicated.
    sequences: Optional[FilterConfig] A object for filters to filter out sequences ending up with a list of sequences to replicate. If not provided all sequences in the named schema will be replicated.
    schema_name: Optional[str] The schema to operate on. Defaults to "public".
    """

    db: str
    dc: str
    src: Optional[DbConfig] = None
    dst: Optional[DbConfig] = None
    tables: Optional[FilterConfig] = None
    sequences: Optional[FilterConfig] = None
    schema_name: Optional[str] = "public"

    _not_empty = field_validator("db", "dc")(not_empty)

    @property
    def file(self) -> str:
        return config_file(self.db, self.dc)

    @property
    def dir(self) -> str:
        return config_dir(self.db, self.dc)

    async def save(self):
        """
        Write the configuration out to disk
        """
        logger = get_logger(self.db, self.dc, "config")
        logger.debug("Caching config to disk...")

        try:
            await makedirs(self.dir)
        except FileExistsError:
            pass

        try:
            await remove(self.file)
        except FileNotFoundError:
            pass

        async with aopen(self.file, "w") as f:
            await f.write(self.model_dump_json(indent=4))

        logger.info("Cached config to disk.")

    @classmethod
    async def load(cls, db: str, dc: str) -> Optional[DbupgradeConfig]:
        """
        Load the specified configuration from disk if present.
        If the existing config is invalid or does not exist return None.
        """
        logger = get_logger(db, dc, "config")
        logger.debug("Trying to load cached config...")

        try:
            async with aopen(config_file(db, dc), "r") as f:
                raw = await f.read()
        except FileNotFoundError:
            logger.info("No cached config available")
            return None

        try:
            out = cls.model_validate_json(raw)
        except ValidationError:
            logger.info("Cached config was not a valid DbupgradeConfig")
            return None

        logger.info("Found cached config.")

        return out
