import asyncio
from viam.module.module import Module
from models.adam_4117 import Adam4117 as Adam4117Model


if __name__ == '__main__':
    asyncio.run(Module.run_from_registry())
