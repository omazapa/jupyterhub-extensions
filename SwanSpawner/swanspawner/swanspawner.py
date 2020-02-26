# Author: Danilo Piparo, Enric Tejedor, Diogo Castro 2015
# Copyright CERN

"""CERN Specific Spawner class"""

import re, json
import os, pwd, subprocess
import time
from tornado import gen
from traitlets import (
    Unicode,
    Bool,
    Int,
    Dict
)

from jinja2 import Environment, FileSystemLoader

import contextlib
import random
import psutil
from socket import (
    socket,
    SO_REUSEADDR,
    SOL_SOCKET,
    gethostname,
)


def define_SwanSpawner_from(base_class):
    """
        The Spawner need to inherit from a proper upstream Spawner (i.e Docker or Kube).
        But since our personalization, added on top of those, is exactly the same for all,
        by allowing a dynamic inheritance we can re-use the same code on all cases.
        This function returns our SwanSpawner, inheriting from a class (upstream Spawner)
        given as parameter.
    """

    class SwanSpawner(base_class):

        options_form_config = Unicode(
            config=True,
            help='Path to configuration file for options_form rendering.'
        )

        lcg_view_path = Unicode(
            default_value='/cvmfs/sft.cern.ch/lcg/views',
            config=True,
            help='Path where LCG views are stored in CVMFS.'
        )

        auth_script = Unicode(
            default_value='',
            config=True,
            help='Script to authenticate.'
        )

        hadoop_auth_script = Unicode(
            config=True,
            help='Script to authenticate with hadoop clusters.'
        )

        init_k8s_user = Unicode(
            config=True,
            help='Script to authenticate with k8s clusters.'
        )

        local_home = Bool(
            default_value=False,
            config=True,
            help="If True, a physical directory on the host will be the home and not eos."
        )

        eos_path_format = Unicode(
            default_value='/eos/user/{username[0]}/{username}/',
            config=True,
            help='Path format of the users home folder in EOS.'
        )
        
        yarn_config_script = Unicode(
            default_value='/cvmfs/sft.cern.ch/lcg/etc/hadoop-confext/hadoop-setconf.sh',
            config=True,
            help='Path in CVMFS of the script to configure a YARN cluster.'
        )

        k8s_config_script = Unicode(
            default_value='/cvmfs/sft.cern.ch/lcg/etc/hadoop-confext/k8s-setconf.sh',
            config=True,
            help='Path in CVMFS of the script to configure a K8s cluster.'
        )

        spark_max_sessions = Int(
            default_value=1,
            config=True,
            help='Number of parallel Spark sessions per user session (container).'
        )

        spark_session_num_ports = Int(
            default_value=3,
            config=True,
            help='Number of ports opened per user session (container).'
        )

        spark_session_port_range_start = Int(
            default_value=5001,
            config=True,
            help='Start of the port range that is used by the user session (container).'
        )

        spark_session_port_range_end = Int(
            default_value=5300,
            config=True,
            help='End of the port range that is used by the user session (container).'
        )

        shared_volumes = Dict(
            config=True,
            help='Volumes to be mounted with a "shared" tag. This allows mount propagation.',
        )

        extra_env = Dict(
            config=True,
            help='Extra environment variables to pass to the container',
        )

        check_cvmfs_status = Bool(
            default_value=True,
            config=True,
            help="Check if CVMFS is accessible. It only works if CVMFS is mounted in the host (not the case in ScienceBox)."
        )

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.options_form = self.render_options_form
            
        @staticmethod
        def get_user_gid(username):
            return str(pwd.getpwnam(username).pw_gid)

        @staticmethod
        def get_user_id(username):
            return str(pwd.getpwnam(username).pw_uid)

        @staticmethod
        def get_hostname():
            return gethostname().split('.')[0]

        def get_lcg_view_path(self):
            return self.lcg_view_path

        def get_lcg_release(self):
            return self.user_options['LCG-rel']

        def get_lcg_platform(self):
            return self.user_options['platform']

        def get_user_env_script_path(self):
            return self.user_options['scriptenv']

        def get_user_cores(self):
            return self.user_options['ncores']

        def get_user_memory(self):
            return self.user_options['memory']

        def get_spark_cluster(self):
            return self.user_options['spark-cluster']

        def get_options_form_config(self):
            if self.options_form_config:
                options_form_config = self.options_form_config
            else:
                options_form_config = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'templates',
                    'options_form_default_config.json')

            try:
                with open(options_form_config) as json_file:
                    return json.load(json_file)
            except Exception as ex:
                self.log.error("Could not initialize form: %s", ex, exc_info=True)
                raise RuntimeError(
                    """
                    Could not initialize form, invalid format
                    """)

        def render_options_form(self, spawner):
            templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
            env = Environment(loader=FileSystemLoader(templates_dir))
            template = env.get_template('options_form_template.html')
            return template.render(options_form_config=self.get_options_form_config())

        def options_from_form(self, formdata):
            """ Fills self.user_options with required fields.
            The fields in the form are based on swanspawner/templates/options_form_template.html
            """
            options = {}
            options['LCG-rel']         = formdata['LCG-rel'][0]
            options['platform']         = formdata['platform'][0]
            options['scriptenv'] = formdata['scriptenv'][0]
            options['ncores']          = int(formdata['ncores'][0])
            options['memory']           = formdata['memory'][0] + 'G'
            options['spark-cluster']   = formdata['spark-cluster'][0] if 'spark-cluster' in formdata.keys() else 'none'

            return options

        def get_env(self):
            """
            Set base environmental variables
            """
            env = super().get_env()

            if self.local_home:
                homepath = "/scratch/%s" %(self.user.name)
            else:
                homepath = self.eos_path_format.format(username = self.user.name)

            env.update(dict(
                ROOT_LCG_VIEW_NAME     = self.get_lcg_release(),
                ROOT_LCG_VIEW_PLATFORM = self.get_lcg_platform(),
                USER_ENV_SCRIPT        = self.get_user_env_script_path(),
                ROOT_LCG_VIEW_PATH     = self.get_lcg_view_path(),
                USER                   = self.user.name,
                USER_ID                = self.get_user_id(self.user.name),
                USER_GID               = self.get_user_gid(self.user.name),
                HOME                   = homepath,
                EOS_PATH_FORMAT        = self.eos_path_format,
                SERVER_HOSTNAME        = os.uname().nodename,

                JPY_USER               = self.user.name,
                JPY_COOKIE_NAME        = self.user.server.cookie_name,
                JPY_BASE_URL           = self.user.base_url,
                JPY_HUB_PREFIX         = self.hub.base_url,
                JPY_HUB_API_URL        = self.hub.api_url
            ))

            if self.extra_env:
                env.update(self.extra_env)

            # Only dockerspawner has these vars, and for now is the only one that needs this
            # code since we still don't have spark ready to work with a kubernetes deployment
            if hasattr(self, 'extra_host_config') and hasattr(self, 'extra_create_kwargs'):
                # Clear old state
                self.extra_host_config['port_bindings'] = {}
                self.extra_create_kwargs['ports'] = []

                # Avoid overriding the default container output port, defined by the Spawner
                if not self.use_internal_ip:
                    self.extra_host_config['port_bindings'][self.port] = (self.host_ip,)

                cluster = self.get_spark_cluster()
                if cluster != 'none':

                    env['SPARK_CLUSTER_NAME'] 		    = cluster
                    env['SPARK_USER'] 		            = self.user.name
                    env['MAX_MEMORY']         	   	    = self.get_user_memory()

                    if cluster == 'k8s':
                        env['SPARK_CONFIG_SCRIPT'] = self.k8s_config_script
                    else:
                        env['SPARK_CONFIG_SCRIPT'] = self.yarn_config_script

                    # Asks the OS for random ports to give them to Docker,
                    # so that Spark can be exposed to the outside
                    # Reserves the ports so that other processes don't use them
                    # before Docker opens them
                    spark_ports = []
                    for _ in range(self.spark_session_num_ports * self.spark_max_sessions):
                        try:
                            reserved_port =  self.get_reserved_port(self.spark_session_port_range_start, self.spark_session_port_range_end)
                        except Exception as ex:
                            self.log.error("Error while allocating ports for Spark: %s", ex, exc_info=True)
                            raise RuntimeError("Error while allocating ports for Spark. Please try again.")
                        self.extra_host_config['port_bindings'][reserved_port] = reserved_port
                        self.extra_create_kwargs['ports'].append(reserved_port)
                        spark_ports.append(str(reserved_port))
                    env["SPARK_PORTS"] = ",".join(spark_ports)

            return env

        @gen.coroutine
        def stop(self, now=False):
            """ Overwrite default spawner to report stop of the container """

            if self._spawn_future and not self._spawn_future.done():
                # Return 124 (timeout) exit code as container got stopped by jupyterhub before successful spawn
                container_exit_code = "124"
            else:
                # Return 0 exit code as container got stopped after spawning correctly
                container_exit_code = "0"

            stop_result = yield super().stop(now)

            self._log_metric(
                self.user.name,
                self.get_hostname(),
                ".".join(["exit_container", "exit_code"]),
                container_exit_code
            )

            return stop_result

        @gen.coroutine
        def poll(self):
            """ Overwrite default poll to get status of container """
            container_exit_code = yield super().poll()

            # None if single - user process is running.
            # Integer exit code status, if it is not running and not stopped by JupyterHub.
            if container_exit_code is not None:
                exit_return_code = str(container_exit_code)
                if exit_return_code.isdigit():
                    value_cleaned = exit_return_code
                else:
                    result = re.search('ExitCode=(\d+)', exit_return_code)
                    if not result:
                        raise Exception("unknown exit code format for this Spawner")
                    value_cleaned = result.group(1)

                self._log_metric(
                    self.user.name,
                    self.get_hostname(),
                    ".".join(["exit_container", "exit_code"]),
                    value_cleaned
                )

            return container_exit_code

        @gen.coroutine
        def start(self):
            """Start the container and perform the operations necessary for mounting
            EOS, authenticating HDFS and authenticating K8S.
            """

            username = self.user.name
            platform = self.get_lcg_platform()
            lcg_rel = self.get_lcg_release()
            cluster = self.get_spark_cluster()
            cpu_quota = self.get_user_cores()
            mem_limit = self.get_user_memory()

            try:
                start_time_configure_user = time.time()

                if not self.local_home and self.auth_script:
                    # When using CERNBox as home, obtain credentials for the user
                    subprocess.call(['sudo', self.auth_script, username], timeout=60)
                    self.log.debug("We are in SwanSpawner. Credentials for %s were requested.", username)

                if self.check_cvmfs_status and not os.path.exists(self.lcg_view_path):
                    raise RuntimeError(
                        """
                        Could not initialize software stack, please <a href="https://cern.ch/ssb" target="_blank">check service status</a> or <a href="https://cern.service-now.com/service-portal/function.do?name=swan" target="_blank">report an issue</a>
                        """
                    )

                if self.check_cvmfs_status and not os.path.exists(self.lcg_view_path + '/' + lcg_rel + '/' + platform):
                    raise ValueError(
                        """
                        Configuration not available: please select other <b>Software stack</b> and <b>Platform</b>.
                        """
                    )

                self._log_metric(
                    self.user.name,
                    self.get_hostname(),
                    ".".join(["configure_user", "duration_sec"]),
                    time.time() - start_time_configure_user
                )

                # If the user selects a Spark Cluster we need to generate a token to allow him in
                if cluster != 'none':
                    start_time_configure_spark = time.time()

                    # FIXME: temporaly limit Cloud Container to specific platform and software stack
                    if cluster == 'k8s' and ("dev" not in lcg_rel and "LCG_96" not in lcg_rel):
                        raise ValueError(
                            """
                            Configuration unsupported: 
                            only <b>Software stack: Only LCG_96 Python2/Python3 or Bleeding Edge Python2/Python3</b> are supported for Cloud Containers
                            """
                        )

                    # If the user selects a Spark Cluster we need to generate some tokens
                    # FIXME: Dont hardcode hadoop path, use hadoop_host_path and hadoop_container_path
                    hadoop_host_path = '/spark/' + username
                    hadoop_container_path = '/spark'

                    # Ensure that env variables are properly cleared
                    self.env.pop('WEBHDFS_TOKEN', None)
                    self.env.pop('HADOOP_TOKEN_FILE_LOCATION', None)
                    self.env.pop('KUBECONFIG', None)

                    # Set authentication and authorization for the user
                    if cluster == 'k8s':
                        subprocess.call([
                            'sudo',
                            self.init_k8s_user,
                            username
                        ], timeout=60)

                        # set location of user kubeconfig for Spark
                        if os.path.exists(hadoop_host_path + '/k8s-user.config'):
                            self.env['KUBECONFIG'] = hadoop_container_path + '/k8s-user.config'
                        else:
                            raise RuntimeError(
                                """
                                Problem connecting to Cloud Containers cluster. 
                                Please <a href="https://cern.service-now.com/service-portal/function.do?name=swan" target="_blank">report an issue</a>
                                """
                            )

                        subprocess.call([
                            'sudo',
                            self.hadoop_auth_script,
                            'analytix',
                            username
                        ], timeout=60)

                        # Set default EOS krb5 cache location to hadoop container path for k8s
                        self.env['KRB5CCNAME'] = hadoop_container_path + '/krb5cc'
                    else:
                        subprocess.call([
                            'sudo',
                            self.hadoop_auth_script,
                            cluster,
                            username
                        ], timeout=60)

                        # Set default location for krb5cc in tmp directory for yarn
                        self.env['KRB5CCNAME'] = '/tmp/krb5cc'

                    # set location of hadoop token file and webhdfs token for Spark
                    if os.path.exists(hadoop_host_path + '/hadoop.toks') and os.path.exists(hadoop_host_path + '/webhdfs.toks'):
                        self.env['HADOOP_TOKEN_FILE_LOCATION'] = hadoop_container_path + '/hadoop.toks'
                        with open(hadoop_host_path + '/webhdfs.toks', 'r') as webhdfs_token_file:
                            self.env['WEBHDFS_TOKEN'] = webhdfs_token_file.read()
                    else:
                        if cluster == 'hadoop-nxcals':
                            raise ValueError(
                                """
                                Access to the NXCALS cluster is not granted. 
                                Please <a href="https://wikis.cern.ch/display/NXCALS/Data+Access+User+Guide#DataAccessUserGuide-nxcals_access" target="_blank">request access</a>
                                """
                            )
                        elif cluster == 'k8s':
                            # if there is no HADOOP_TOKEN_FILE or WEBHDFS_TOKEN with K8s we ignore (no HDFS access granted)
                            pass
                        else:
                            # yarn clusters require HADOOP_TOKEN_FILE and WEBHDFS_TOKEN containing YARN and HDFS tokens
                            raise ValueError(
                                """
                                Access to the Analytix cluster is not granted. 
                                Please <a href="https://cern.service-now.com/service-portal/report-ticket.do?name=request&fe=Hadoop-Components" target="_blank">request access</a>
                                """
                            )

                    self._log_metric(
                        self.user.name,
                        self.get_hostname(),
                        ".".join(["configure_spark", lcg_rel, cluster, "duration_sec"]),
                        time.time() - start_time_configure_spark
                    )

                # The bahaviour changes if this if dockerspawner or kubespawner
                if hasattr(self, 'extra_host_config'):
                    # Due to dockerpy limitations in the current version, we cannot use --cpu to limit cpu.
                    # This is an alternative (and old) way of doing it
                    self.extra_host_config.update({
                        'cpu_period' : 100000,
                        'cpu_quota' : 100000 * cpu_quota
                    })
                else:
                    self.cpu_limit = cpu_quota
                self.mem_limit = mem_limit

                # Enabling GPU for cuda stacks
                # Options to export nvidia device can be found in https://github.com/NVIDIA/nvidia-container-runtime#nvidia_require_
                if "cu" in lcg_rel:
                    self.env['NVIDIA_VISIBLE_DEVICES']='all'  # We are making visible all the devices, if the host has more that one can be used.
                    self.env['NVIDIA_DRIVER_CAPABILITIES']='compute,utility'
                    self.env['NVIDIA_REQUIRE_CUDA']='cuda>=10.0 driver>=410'
                    if hasattr(self, 'extra_host_config'): # for docker but not for kuberneters
                        self.extra_host_config.update({'runtime' : 'nvidia'})
                    if hasattr(self, 'extra_resource_guarantees'): # for kubernetes but not for docker
                        self.extra_resource_guarantees = {"nvidia.com/gpu": "1" }
                    if hasattr(self, 'extra_resource_limits'): # only 1 gpu per user allowed (for kubernetes)
                        self.extra_resource_limits = {"nvidia.com/gpu": "1"}

                start_time_start_container = time.time()

                # start configured container
                startup = yield super().start()

                # log container start success metrics
                self._log_metric(
                    self.user.name,
                    self.get_hostname(),
                    ".".join(["start_container", lcg_rel, "duration_sec"]),
                    time.time() - start_time_start_container
                )

                return startup
            except BaseException as e:
                self.log.error("Error while spawning the user container: %s", e, exc_info=True)
                raise e

        @property
        def volume_mount_points(self):
            """
            Override this method to take into account the "shared" volumes.
            """
            return self.get_volumes(only_mount=True)


        @property
        def volume_binds(self):
            """
            Since EOS now uses autofs (mounts and unmounts mount points automatically), we have to mount
            eos in the container with the propagation option set to "shared".
            This means that: when users try to access /eos/projects, the endpoint will be mounted automatically,
            and made available in the container without the need to restart the session (it gets propagated).
            This also means that, if the user endpoint fails, when it gets back up it will be made available in
            the container without the need to restart the session.
            The Spawnwer/dockerpy do not support this option. But, if volume_bins return a list of string, they will
            pass the list forward until the container construction, without checking or trying to manipulate the list.
            """
            return self.get_volumes()

        def get_volumes(self, only_mount=False):

            def _fmt(v):
                return self.format_volume_name(v, self)

            def _convert_list(volumes, binds, mode="rw"):
                for k, v in volumes.items():
                    m = mode
                    if isinstance(v, dict):
                        if "mode" in v:
                            m = v["mode"]
                        v = v["bind"]

                    if only_mount:
                        binds.append(_fmt(v))
                    else:
                        binds.append("%s:%s:%s" % (_fmt(k), _fmt(v), m))
                return binds

            binds = _convert_list(self.volumes, [])
            binds = _convert_list(self.read_only_volumes, binds, mode="ro")
            return _convert_list(self.shared_volumes, binds, mode="shared")

        @staticmethod
        def get_reserved_port(start, end, n_tries=10):
            """
                Reserve a random available port.
                It puts the door in TIME_WAIT state so that no other process gets it when asking for a random port,
                but allows processes to bind to it, due to the SO_REUSEADDR flag.
                From https://github.com/Yelp/ephemeral-port-reserve
            """
            for i in range(n_tries):
                try:
                    with contextlib.closing(socket()) as s:
                        port = random.randint(start, end)
                        net_connections = psutil.net_connections()
                        # look through the list of active connections to check if the port is being used or not and return FREE if port is unused
                        if next((conn.laddr[1] for conn in net_connections if conn.laddr[1] == port),'FREE') != 'FREE':
                            raise Exception('Port {} is in use'.format(port))
                        s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
                        s.bind(('127.0.0.1', port))

                        # the connect below deadlocks on kernel >= 4.4.0 unless this arg is greater than zero
                        s.listen(1)

                        sockname = s.getsockname()

                        # these three are necessary just to get the port into a TIME_WAIT state
                        with contextlib.closing(socket()) as s2:
                            s2.connect(sockname)
                            s.accept()
                            return sockname[1]
                except:
                    if i == n_tries - 1:
                        raise

        def _log_metric(self, user, host, metric, value):
            self.log.info("user: %s, host: %s, metric: %s, value: %s" % (user, host, metric, value))

    return SwanSpawner