import logging

from fastapi import FastAPI

from app.api import router
from app.repositories import create_db_and_tables


logging.basicConfig(level=logging.INFO)
create_db_and_tables()

app = FastAPI()
app.include_router(router)
