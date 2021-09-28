import uvicorn
from fastapi import FastAPI, WebSocket, BackgroundTasks, HTTPException, Depends
from pydantic import BaseModel
from fastapi.encoders import jsonable_encoder
from typing import List, Optional, Dict, Any, Union

import json, os, io

from .services import filesystem_service
from .services import dbt_service
from .services import task_service
from .logging import GLOBAL_LOGGER as logger

# ORM stuff
from sqlalchemy.orm import Session
from . import crud, models, schemas
from .database import SessionLocal, engine

app = FastAPI()


class UnparsedManifestBlob(BaseModel):
    state_id: str
    body: str

class State(BaseModel):
    state_id: str


class RunArgs(BaseModel):
    state_id: str
    models: List[str] = None
    exclude: List[str] = None
    single_threaded: bool = False
    state: str = None
    selector_name: str = None
    defer: bool = None
    threads: int = 4

class SQLConfig(BaseModel):
    state_id: str
    sql: str

@app.get("/")
async def test(tasks: BackgroundTasks):
    return {"abc": 123, "tasks": tasks.tasks}

@app.post("/push")
async def push_unparsed_manifest(manifest: UnparsedManifestBlob):
    # Parse / validate it
    state_id = manifest.state_id
    body = manifest.body

    logger.info(f"Recieved manifest {len(body)} bytes")

    path = filesystem_service.get_root_path(state_id)
    reuse = True

    # Stupid example of reusing an existing manifest
    if not os.path.exists(path):
        reuse = False
        unparsed_manifest_dict = json.loads(body)
        filesystem_service.write_unparsed_manifest_to_disk(state_id, unparsed_manifest_dict)

    # Write messagepack repr to disk
    # Return a key that the client can use to operate on it?
    return {
        "ok": True,
        "state": state_id,
        "bytes": len(body),
        "reuse": reuse,
        "path": path,
    }


@app.post("/parse")
def parse_project(state: State):
    state_id = state.state_id
    path = filesystem_service.get_root_path(state_id)
    serialize_path = filesystem_service.get_path(state_id, 'manifest.msgpack')

    logger.info(f"Parsing manifest from filetree")
    manifest = dbt_service.parse_to_manifest(path)

    logger.info("Serializing as messagepack file")
    dbt_service.serialize_manifest(manifest, serialize_path)

    return {"parsing": state.state_id, "path": serialize_path}


@app.post("/run")
async def run_models(args: RunArgs):
    path = filesystem_service.get_root_path(args.state_id)
    serialize_path = filesystem_service.get_path(args.state_id, 'manifest.msgpack')

    manifest = dbt_service.deserialize_manifest(serialize_path)
    results = dbt_service.dbt_run_sync(path, args, manifest)

    encoded_results = jsonable_encoder(results)

    return {
        "parsing": args.state_id,
        "path": serialize_path,
        "res": encoded_results,
        "ok": True,
    }

@app.post("/run-async")
async def run_models(
    args: RunArgs,
    background_tasks: BackgroundTasks,
    response_model=schemas.Task,
    db: Session = Depends(crud.get_db)
):
    return task_service.run_async(background_tasks, db, args)


@app.post("/preview")
async def preview_sql(sql: SQLConfig):
    path = filesystem_service.get_root_path(sql.state_id)
    serialize_path = filesystem_service.get_path(sql.state_id, 'manifest.msgpack')

    manifest = dbt_service.deserialize_manifest(serialize_path)
    result = dbt_service.execute_sql(manifest, path, sql.sql)

    return {
        "state": sql.state_id,
        "path": serialize_path,
        "ok": True,
        "res": jsonable_encoder(result),
    }

@app.post("/compile")
async def compile_sql(sql: SQLConfig):
    path = filesystem_service.get_root_path(sql.state_id)
    serialize_path = filesystem_service.get_path(sql.state_id, 'manifest.msgpack')

    manifest = dbt_service.deserialize_manifest(serialize_path)
    result = dbt_service.compile_sql(manifest, path, sql.sql)

    return {
        "state": sql.state_id,
        "path": serialize_path,
        "ok": True,
        "res": jsonable_encoder(result),
    }


class Task(BaseModel):
    task_id: str

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    db: Session = Depends(crud.get_db),
):
    await websocket.accept()
    message = await websocket.receive_text()

    message_data = json.loads(message)
    logger.info(f"Got WS request: {message_data}")

    task_id = message_data['task_id']

    for log_line in task_service.tail_logs_for_path(db, task_id):
        await websocket.send_text(log_line)
    await websocket.close(code=1000)