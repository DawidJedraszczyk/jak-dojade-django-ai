from dataclasses import dataclass
import duckdb
import functools
from pathlib import Path
import pyarrow


@dataclass
class Result:
  rel: duckdb.DuckDBPyRelation

  def one(self):
    return self.rel.fetchone()

  def scalar(self):
    return self.one()[0]

  def arrow(self) -> pyarrow.StructArray:
    return self.rel.arrow().to_struct_array().combine_chunks() # type: ignore

  def np(self):
    return self.rel.fetchnumpy()

  def count(self):
    return len(self.rel)


class Db:
  db: duckdb.DuckDBPyConnection
  scripts: Path

  def __init__(self, path: Path, scripts: Path, write=False, variables={}):
    self.db = duckdb.connect(path, not write)
    self.scripts = scripts

    for k, v in variables.items():
      self.set_variable(k, v)


  def __enter__(self):
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.db.close()


  def set_variable(self, name: str, value):
    self.db.sql(f"set variable {name} = ?", params=[value])

  def get_variable(self, name: str):
    return self.sql(f"select getvariable('{name}')").scalar()


  def sql(self, query: str, params=None, views={}) -> Result:
    for k, v in views.items():
      self.db.register(k, v)

    r = Result(self.db.sql(query, params=params))

    for k in views.keys():
      self.db.unregister(k)

    return r


  def script(self, script_name: str, params=None, views={}) -> Result:
    return self.sql(self.load(script_name), params, views)


  @functools.cache
  def load(self, name: str):
    path = self.scripts
    parts = name.split("/")
    parts[-1] += ".sql"

    for part in parts:
      path = path / part

    with open(path, "r") as file:
        return file.read()
