import re
import serial

from typing import (Any, ClassVar, Dict, List, Mapping, Optional,
                    Sequence, Tuple)

from typing_extensions import Self
from viam.components.sensor import *
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import Geometry, ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import SensorReading, ValueTypes, struct_to_dict


class Adam4117(Sensor, EasyResource):
    # To enable debug-level logging, either run viam-server with the --debug option,
    # or configure your resource/machine to display debug logs.
    MODEL: ClassVar[Model] = Model(
        ModelFamily("brad-grigsby", "Advantech"), "adam-4117"
    )

    serial_conn: Optional[serial.Serial] = None
    port: str
    address: str
    baud: int
    channels: List[int]

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """This method creates a new instance of this Sensor component.
        The default implementation sets the name from the `config` parameter.

        Args:
            config (ComponentConfig): The configuration for this resource
            dependencies (Mapping[ResourceName, ResourceBase]): The dependencies (both required and optional)

        Returns:
            Self: The resource
        """
        sensor = cls(config.name)
        sensor.reconfigure(config, dependencies)

        return sensor

    @classmethod
    def validate_config(
        cls, config: ComponentConfig
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """This method allows you to validate the configuration object received from the machine,
        as well as to return any required dependencies or optional dependencies based on that `config`.

        Args:
            config (ComponentConfig): The configuration for this resource

        Returns:
            Tuple[Sequence[str], Sequence[str]]: A tuple where the
                first element is a list of required dependencies and the
                second element is a list of optional dependencies
        """
        attributes = struct_to_dict(config.attributes)
        if not attributes.get("port"):
            raise Exception("port is required")
        if not attributes.get("address"):
            raise Exception("address is required")
        if not attributes.get("baud"):
            raise Exception("baud is required")
        if not attributes.get("channels"):
            raise Exception("channels is required")
        
        return ([], [])
    
    def reconfigure(
        self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> None:
        """This method allows you to dynamically update your service when it receives a new `config` object.

        Args:
            config (ComponentConfig): The new configuration
            dependencies (Mapping[ResourceName, ResourceBase]): Any dependencies (both implicit and explicit)
        """
        attributes = struct_to_dict(config.attributes)

        self.logger.info("Starting reconfigure with attributes: %s", attributes)
        self.port = attributes.get("port")
        self.address = attributes.get("address")
        self.baud = attributes.get("baud")
        self.channels = attributes.get("channels", [])
        self._open_serial()
    
    def _open_serial(self):
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = serial.Serial(self.port, int(self.baud), timeout=0.05, inter_byte_timeout=0.01)
        self.logger.info(f"Opened {self.port} at {self.baud} baud")

    def _read_all_channels(self) -> List[float]:
        cmd = f'#{self.address}\r'.encode()
        self.serial_conn.write(cmd)
        response = self.serial_conn.read_until(b'\r').decode().strip()
        matches = re.findall(r'[+-]\d+\.\d+', response)

        return [float(v) for v in matches]

    async def get_readings(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, SensorReading]:
        try:
            values = self._read_all_channels()
        except (serial.SerialException, OSError) as e:
            self.logger.warning(f"Serial error: {e}")
            self._open_serial()
            values = self._read_all_channels()

        readings = {}
        for ch in self.channels:
            ch = int(ch)
            if 0 <= ch < len(values):
                readings[f"channel_{ch}"] = values[ch]
            else:
                readings[f"channel_{ch}"] = 0.0
        
        return readings
    
    # async def close(self):
    #     if self.serial_conn:
    #         self.serial_conn.close()

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, ValueTypes]:
        self.logger.error("`do_command` is not implemented")
        raise NotImplementedError()

    async def get_status(
        self, *, timeout: Optional[float] = None, **kwargs
    ) -> Mapping[str, ValueTypes]:
        self.logger.error("`get_status` is not implemented")
        raise NotImplementedError()

    async def get_geometries(
        self, *, extra: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Sequence[Geometry]:
        self.logger.error("`get_geometries` is not implemented")
        raise NotImplementedError()

