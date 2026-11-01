"""Ownership-bound routing for native Shepherd source references."""
import copy

from .store import Conflict, InvalidState


class SourceResolver:
    """Route source refs only to explicitly registered, owned connectors."""

    def __init__(self, connectors):
        self._connectors = {}
        for connector in connectors:
            key = (getattr(connector, 'engine', None),
                   getattr(connector, 'native_session_id', None))
            if not all(isinstance(value, str) and value for value in key):
                raise InvalidState('Shepherd Resolver Connector 身份无效')
            if key in self._connectors:
                raise Conflict('Shepherd Resolver Connector 身份重复')
            if not callable(getattr(connector, 'resolve_source', None)):
                raise InvalidState('Shepherd Connector 不支持来源回读')
            self._connectors[key] = connector

    def resolve(self, source_ref):
        return self.resolve_with_input(source_ref, None)

    def resolve_batch(self, requests):
        """Resolve one bounded, same-source batch with caller-owned public values.

        Native batch readers allocate public values for this call only. Legacy
        connectors may return shared values, so isolate those at this boundary.
        No reader or parsed source survives the operation (including a fence).
        """
        if not requests:
            return []
        connectors = []
        for source_ref, _trusted in requests:
            if not isinstance(source_ref, dict):
                raise InvalidState('Shepherd 来源引用无效')
            connector = self._connectors.get((source_ref.get('engine'), source_ref.get('session_id')))
            if connector is None:
                raise Conflict('Shepherd 来源 Session 未登记或不受信任')
            connectors.append(connector)
        connector = connectors[0]
        if any(item is not connector for item in connectors):
            raise InvalidState('Shepherd 来源批次必须属于同一 Session')
        read = getattr(connector, 'resolve_sources', None)
        if callable(read):
            refs = [source for source, _trusted in requests]
            kwargs = ({'trusted_publics': [trusted for _source, trusted in requests]}
                      if connector.engine == 'codex' else {})
            results = read(refs, **kwargs)
        else:
            results = []
            for source, trusted in requests:
                result = self.resolve_with_input(source, trusted)
                results.append({'source': result['source'], 'public': copy.deepcopy(result['public'])})
        if not isinstance(results, list) or len(results) != len(requests):
            raise InvalidState('Shepherd Connector 来源批次长度不匹配')
        if any(not isinstance(row, dict) or set(row) != {'source', 'public'} for row in results):
            raise InvalidState('Shepherd Connector 来源回读格式无效')
        return results

    def resolve_with_input(self, source_ref, trusted_public):
        if not isinstance(source_ref, dict):
            raise InvalidState('Shepherd 来源引用无效')
        connector = self._connectors.get((source_ref.get('engine'),
                                          source_ref.get('session_id')))
        if connector is None:
            raise Conflict('Shepherd 来源 Session 未登记或不受信任')
        if trusted_public is not None and getattr(connector, 'engine', None) == 'codex':
            result = connector.resolve_source(copy.deepcopy(source_ref),
                                              trusted_public=copy.deepcopy(trusted_public))
        else:
            result = connector.resolve_source(copy.deepcopy(source_ref))
        if not isinstance(result, dict) or set(result) != {'source', 'public'}:
            raise InvalidState('Shepherd Connector 来源回读格式无效')
        return result
