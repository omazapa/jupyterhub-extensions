from .swanspawner import define_SwanSpawner_from
from kubespawner import KubeSpawner

from tornado import gen

class SwanKubeSpawner(define_SwanSpawner_from(KubeSpawner)):

    @gen.coroutine
    def start(self):
        """Perform the operations necessary for GPU support
        """

        try:
            # Enabling GPU for cuda stacks
            # Options to export nvidia device can be found in https://github.com/NVIDIA/nvidia-container-runtime#nvidia_require_
            if "cu" in self.user_options[self.lcg_rel_field]:
                self.env['NVIDIA_VISIBLE_DEVICES'] = 'all'  # We are making visible all the devices, if the host has more that one can be used.
                self.env['NVIDIA_DRIVER_CAPABILITIES'] = 'compute,utility'
                self.env['NVIDIA_REQUIRE_CUDA'] = 'cuda>=10.0 driver>=410'
                self.extra_resource_guarantees = {"nvidia.com/gpu": "1"}
                self.extra_resource_limits = {"nvidia.com/gpu": "1"}

            self.cpu_limit = self.user_options[self.user_n_cores]
            self.mem_limit = self.user_options[self.user_memory]

            # start configured container
            startup = yield super().start()

            return startup
        except BaseException as e:
            self.log.error("Error while spawning the user container: %s", e, exc_info=True)
            raise e