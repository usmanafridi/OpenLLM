"""This type stub file was generated by pyright."""

from .resource import Collection
from .resource import Model

class Node(Model):
    """A node in a swarm."""

    id_attribute = ...
    @property
    def version(self):
        """The version number of the service. If this is not the same as the
        server, the :py:meth:`update` function will not work and you will
        need to call :py:meth:`reload` before calling it again.
        """
        ...
    def update(self, node_spec):
        """Update the node's configuration.

        Args:
            node_spec (dict): Configuration settings to update. Any values
                not provided will be removed. Default: ``None``

        Returns:
            `True` if the request went through.

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.

        Example:
            >>> node_spec = {'Availability': 'active',
                             'Name': 'node-name',
                             'Role': 'manager',
                             'Labels': {'foo': 'bar'}
                            }
            >>> node.update(node_spec)

        """
        ...
    def remove(self, force=...):
        """Remove this node from the swarm.

        Args:
            force (bool): Force remove an active node. Default: `False`

        Returns:
            `True` if the request was successful.

        Raises:
            :py:class:`docker.errors.NotFound`
                If the node doesn't exist in the swarm.

            :py:class:`docker.errors.APIError`
                If the server returns an error.
        """
        ...

class NodeCollection(Collection):
    """Nodes on the Docker server."""

    model = Node
    def get(self, node_id):  # -> Model:
        """Get a node.

        Args:
            node_id (string): ID of the node to be inspected.

        Returns:
            A :py:class:`Node` object.

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.
        """
        ...
    def list(self, *args, **kwargs):  # -> list[Model]:
        """List swarm nodes.

        Args:
            filters (dict): Filters to process on the nodes list. Valid
                filters: ``id``, ``name``, ``membership`` and ``role``.
                Default: ``None``

        Returns:
            A list of :py:class:`Node` objects.

        Raises:
            :py:class:`docker.errors.APIError`
                If the server returns an error.

        Example:
            >>> client.nodes.list(filters={'role': 'manager'})
        """
        ...
