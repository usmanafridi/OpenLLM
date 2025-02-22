"""This type stub file was generated by pyright."""

from ..utils import check_resource
from ..utils import minimum_version

class NetworkApiMixin:
    def networks(self, names=..., ids=..., filters=...):
        """List networks. Similar to the ``docker network ls`` command.

        Args:
            names (:py:class:`list`): List of names to filter by
            ids (:py:class:`list`): List of ids to filter by
            filters (dict): Filters to be processed on the network list.
                Available filters:
                - ``driver=[<driver-name>]`` Matches a network's driver.
                - ``label=[<key>]``, ``label=[<key>=<value>]`` or a list of
                    such.
                - ``type=["custom"|"builtin"]`` Filters networks by type.

        Returns:
            (dict): List of network objects.

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.
        """
        ...
    def create_network(
        self,
        name,
        driver=...,
        options=...,
        ipam=...,
        check_duplicate=...,
        internal=...,
        labels=...,
        enable_ipv6=...,
        attachable=...,
        scope=...,
        ingress=...,
    ):
        """Create a network. Similar to the ``docker network create``.

        Args:
            name (str): Name of the network
            driver (str): Name of the driver used to create the network
            options (dict): Driver options as a key-value dictionary
            ipam (IPAMConfig): Optional custom IP scheme for the network.
            check_duplicate (bool): Request daemon to check for networks with
                same name. Default: ``None``.
            internal (bool): Restrict external access to the network. Default
                ``False``.
            labels (dict): Map of labels to set on the network. Default
                ``None``.
            enable_ipv6 (bool): Enable IPv6 on the network. Default ``False``.
            attachable (bool): If enabled, and the network is in the global
                scope,  non-service containers on worker nodes will be able to
                connect to the network.
            scope (str): Specify the network's scope (``local``, ``global`` or
                ``swarm``)
            ingress (bool): If set, create an ingress network which provides
                the routing-mesh in swarm mode.

        Returns:
            (dict): The created network reference object

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.

        Example:
            A network using the bridge driver:

                >>> client.api.create_network("network1", driver="bridge")

            You can also create more advanced networks with custom IPAM
            configurations. For example, setting the subnet to
            ``192.168.52.0/24`` and gateway address to ``192.168.52.254``.

            .. code-block:: python

                >>> ipam_pool = docker.types.IPAMPool(
                    subnet='192.168.52.0/24',
                    gateway='192.168.52.254'
                )
                >>> ipam_config = docker.types.IPAMConfig(
                    pool_configs=[ipam_pool]
                )
                >>> client.api.create_network("network1", driver="bridge",
                                                 ipam=ipam_config)
        """
        ...
    @minimum_version("1.25")
    def prune_networks(self, filters=...):
        """Delete unused networks.

        Args:
            filters (dict): Filters to process on the prune list.

        Returns:
            (dict): A dict containing a list of deleted network names and
                the amount of disk space reclaimed in bytes.

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.
        """
        ...
    @check_resource("net_id")
    def remove_network(self, net_id):  # -> None:
        """Remove a network. Similar to the ``docker network rm`` command.

        Args:
            net_id (str): The network's id
        """
        ...
    @check_resource("net_id")
    def inspect_network(self, net_id, verbose=..., scope=...):
        """Get detailed information about a network.

        Args:
            net_id (str): ID of network
            verbose (bool): Show the service details across the cluster in
                swarm mode.
            scope (str): Filter the network by scope (``swarm``, ``global``
                or ``local``).
        """
        ...
    @check_resource("container")
    def connect_container_to_network(
        self,
        container,
        net_id,
        ipv4_address=...,
        ipv6_address=...,
        aliases=...,
        links=...,
        link_local_ips=...,
        driver_opt=...,
        mac_address=...,
    ):  # -> None:
        """Connect a container to a network.

        Args:
            container (str): container-id/name to be connected to the network
            net_id (str): network id
            aliases (:py:class:`list`): A list of aliases for this endpoint.
                Names in that list can be used within the network to reach the
                container. Defaults to ``None``.
            links (:py:class:`list`): A list of links for this endpoint.
                Containers declared in this list will be linked to this
                container. Defaults to ``None``.
            ipv4_address (str): The IP address of this container on the
                network, using the IPv4 protocol. Defaults to ``None``.
            ipv6_address (str): The IP address of this container on the
                network, using the IPv6 protocol. Defaults to ``None``.
            link_local_ips (:py:class:`list`): A list of link-local
                (IPv4/IPv6) addresses.
            mac_address (str): The MAC address of this container on the
                network. Defaults to ``None``.
        """
        ...
    @check_resource("container")
    def disconnect_container_from_network(self, container, net_id, force=...):  # -> None:
        """Disconnect a container from a network.

        Args:
            container (str): container ID or name to be disconnected from the
                network
            net_id (str): network ID
            force (bool): Force the container to disconnect from a network.
                Default: ``False``
        """
        ...
