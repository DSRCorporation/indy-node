from typing import List, Optional

from common.serializers.serialization import config_state_serializer, state_roots_serializer
from indy_common.authorize.auth_constraints import ConstraintCreator, ConstraintsSerializer
from indy_common.authorize.auth_actions import AuthActionEdit, AuthActionAdd, EDIT_PREFIX, ADD_PREFIX, split_action_id
from indy_common.config_util import getConfig
from indy_common.state import config
from plenum.common.exceptions import InvalidClientRequest

from plenum.common.txn_util import reqToTxn, is_forced, get_payload_data, append_txn_metadata, get_type
from plenum.server.config_req_handler import ConfigReqHandler as PConfigReqHandler
from plenum.common.constants import TXN_TYPE, NAME, VERSION, FORCE, TXN_AUTHOR_AGREEMENT, TXN_AUTHOR_AGREEMENT_AML
from indy_common.constants import POOL_UPGRADE, START, CANCEL, SCHEDULE, ACTION, POOL_CONFIG, NODE_UPGRADE, PACKAGE, \
    REINSTALL, AUTH_RULE, AUTH_RULES, CONSTRAINT, AUTH_ACTION, OLD_VALUE, NEW_VALUE, AUTH_TYPE, FIELD, GET_AUTH_RULE, \
    RULES
from indy_common.types import Request, ClientGetAuthRuleOperation, AuthRuleField
from indy_node.persistence.idr_cache import IdrCache
from indy_node.server.upgrader import Upgrader
from indy_node.server.pool_config import PoolConfig
from storage.state_ts_store import StateTsDbStorage


class ConfigReqHandler(PConfigReqHandler):
    write_types = \
        {POOL_UPGRADE, NODE_UPGRADE, POOL_CONFIG, AUTH_RULE, AUTH_RULES} | \
        PConfigReqHandler.write_types
    query_types = \
        {GET_AUTH_RULE, } | \
        PConfigReqHandler.query_types

    def __init__(self, ledger, state, domain_state, idrCache: IdrCache,
                 upgrader: Upgrader, poolManager, poolCfg: PoolConfig,
                 write_req_validator, bls_store=None, ts_store: Optional[StateTsDbStorage] = None):
        super().__init__(ledger, state, domain_state, bls_store, ts_store)
        self.idrCache = idrCache
        self.upgrader = upgrader
        self.poolManager = poolManager
        self.poolCfg = poolCfg
        self.write_req_validator = write_req_validator
        self.constraint_serializer = ConstraintsSerializer(config_state_serializer)
        self._add_query_handler(GET_AUTH_RULE, self.handle_get_auth_rule)

    def doStaticValidation(self, request: Request):
        super().doStaticValidation(request)

        identifier, req_id, operation = request.identifier, request.reqId, request.operation
        if operation[TXN_TYPE] == POOL_UPGRADE:
            self._doStaticValidationPoolUpgrade(identifier, req_id, operation)
        elif operation[TXN_TYPE] == POOL_CONFIG:
            self._doStaticValidationPoolConfig(identifier, req_id, operation)
        elif operation[TXN_TYPE] == AUTH_RULE:
            self._doStaticValidationAuthRule(identifier, req_id, operation)
        elif operation[TXN_TYPE] == AUTH_RULES:
            self._doStaticValidationAuthRules(identifier, req_id, operation)
        elif operation[TXN_TYPE] == GET_AUTH_RULE:
            self._doStaticValidationGetAuthRule(identifier, req_id, operation)

    def _doStaticValidationPoolConfig(self, identifier, reqId, operation):
        pass

    def _doStaticValidationAuthRules(self, identifier, reqId, operation):
        for rule in operation[RULES]:
            self._doStaticValidationAuthRule(identifier, reqId, rule)

    def _doStaticValidationAuthRule(self, identifier, reqId, operation):
        try:
            ConstraintCreator.create_constraint(operation.get(CONSTRAINT))
        except ValueError as exp:
            raise InvalidClientRequest(identifier,
                                       reqId,
                                       exp)

        action = operation.get(AUTH_ACTION, None)

        if OLD_VALUE not in operation and action == EDIT_PREFIX:
            raise InvalidClientRequest(identifier, reqId,
                                       "Transaction for change authentication "
                                       "rule for {}={} must contain field {}".
                                       format(AUTH_ACTION, EDIT_PREFIX, OLD_VALUE))

        if OLD_VALUE in operation and action == ADD_PREFIX:
            raise InvalidClientRequest(identifier, reqId,
                                       "Transaction for change authentication "
                                       "rule for {}={} must not contain field {}".
                                       format(AUTH_ACTION, ADD_PREFIX, OLD_VALUE))
        self._check_auth_key(operation, identifier, reqId)

    def _doStaticValidationGetAuthRule(self, identifier, req_id, operation):
        required_fields = list(dict(ClientGetAuthRuleOperation.schema).keys())
        required_fields.remove(OLD_VALUE)
        if len(operation) > 1:
            if not set(required_fields).issubset(set(operation.keys())):
                raise InvalidClientRequest(identifier, req_id,
                                           "Not enough fields to build an auth key.")
            self._check_auth_key(operation, identifier, req_id)

    def _doStaticValidationPoolUpgrade(self, identifier, reqId, operation):
        action = operation.get(ACTION)
        if action not in (START, CANCEL):
            raise InvalidClientRequest(identifier, reqId,
                                       "{} not a valid action".
                                       format(action))
        if action == START:
            schedule = operation.get(SCHEDULE, {})
            force = operation.get(FORCE)
            force = str(force) == 'True'
            isValid, msg = self.upgrader.isScheduleValid(
                schedule, self.poolManager.getNodesServices(), force)
            if not isValid:
                raise InvalidClientRequest(identifier, reqId,
                                           "{} not a valid schedule since {}".
                                           format(schedule, msg))

        # TODO: Check if cancel is submitted before start

    def validate(self, req: Request):
        super().validate(req)

        status = '*'
        operation = req.operation
        typ = operation.get(TXN_TYPE)
        if typ not in self.write_types:
            return
        if typ == POOL_UPGRADE:
            pkg_to_upgrade = req.operation.get(PACKAGE, getConfig().UPGRADE_ENTRY)
            targetVersion = req.operation[VERSION]
            reinstall = req.operation.get(REINSTALL, False)
            # check package name
            if not pkg_to_upgrade:
                raise InvalidClientRequest(req.identifier, req.reqId, "Upgrade package name is empty")

            try:
                res = self.upgrader.check_upgrade_possible(pkg_to_upgrade, targetVersion, reinstall)
            except Exception as exc:
                res = str(exc)

            if res:
                raise InvalidClientRequest(req.identifier, req.reqId, res)

            action = operation.get(ACTION)
            # TODO: Some validation needed for making sure name and version
            # present
            txn = self.upgrader.get_upgrade_txn(
                lambda txn: get_payload_data(txn).get(
                    NAME,
                    None) == req.operation.get(
                    NAME,
                    None) and get_payload_data(txn).get(VERSION) == req.operation.get(VERSION),
                reverse=True)
            if txn:
                status = get_payload_data(txn).get(ACTION, '*')

            if status == START and action == START:
                raise InvalidClientRequest(
                    req.identifier,
                    req.reqId,
                    "Upgrade '{}' is already scheduled".format(
                        req.operation.get(NAME)))
            if status == '*':
                auth_action = AuthActionAdd(txn_type=POOL_UPGRADE,
                                            field=ACTION,
                                            value=action)
            else:
                auth_action = AuthActionEdit(txn_type=POOL_UPGRADE,
                                             field=ACTION,
                                             old_value=status,
                                             new_value=action)
            self.write_req_validator.validate(req,
                                              [auth_action])
        elif typ == POOL_CONFIG:
            action = '*'
            status = '*'
            self.write_req_validator.validate(req,
                                              [AuthActionEdit(txn_type=typ,
                                                              field=ACTION,
                                                              old_value=status,
                                                              new_value=action)])
        elif typ == AUTH_RULE:
            self.write_req_validator.validate(req,
                                              [AuthActionEdit(txn_type=typ,
                                                              field="*",
                                                              old_value="*",
                                                              new_value="*")])
        elif typ == AUTH_RULES:
            self.write_req_validator.validate(req,
                                              [AuthActionEdit(txn_type=typ,
                                                              field="*",
                                                              old_value="*",
                                                              new_value="*")])
        elif typ == TXN_AUTHOR_AGREEMENT:
            self.write_req_validator.validate(req,
                                              [AuthActionAdd(txn_type=typ,
                                                             field="*",
                                                             value="*")])
        elif typ == TXN_AUTHOR_AGREEMENT_AML:
            self.write_req_validator.validate(req,
                                              [AuthActionAdd(txn_type=typ,
                                                             field='*',
                                                             value='*')])

    def authorize(self, req: Request):
        # We don't need authorization from plenum since we have auth map in node
        pass

    def apply(self, req: Request, cons_time):
        txn = append_txn_metadata(reqToTxn(req),
                                  txn_time=cons_time)
        self.ledger.append_txns_metadata([txn])
        (start, _), _ = self.ledger.appendTxns([txn])
        self.updateState([txn], isCommitted=False)
        return start, txn

    def commit(self, txnCount, stateRoot, txnRoot, ppTime) -> List:
        committedTxns = super().commit(txnCount, stateRoot, txnRoot, ppTime)
        for txn in committedTxns:
            # Handle POOL_UPGRADE or POOL_CONFIG transaction here
            # only in case it is not forced.
            # If it is forced then it was handled earlier
            # in applyForced method.
            if not is_forced(txn):
                self.upgrader.handleUpgradeTxn(txn)
                self.poolCfg.handleConfigTxn(txn)
        return committedTxns

    def applyForced(self, req: Request):
        super().applyForced(req)
        txn = reqToTxn(req)
        self.upgrader.handleUpgradeTxn(txn)
        self.poolCfg.handleConfigTxn(txn)

    @staticmethod
    def get_auth_key(operation):
        action = operation.get(AUTH_ACTION, None)
        old_value = operation.get(OLD_VALUE, None)
        new_value = operation.get(NEW_VALUE, None)
        auth_type = operation.get(AUTH_TYPE, None)
        field = operation.get(FIELD, None)

        return AuthActionEdit(txn_type=auth_type,
                              field=field,
                              old_value=old_value,
                              new_value=new_value).get_action_id() \
            if action == EDIT_PREFIX else \
            AuthActionAdd(txn_type=auth_type,
                          field=field,
                          value=new_value).get_action_id()

    @staticmethod
    def get_auth_constraint(operation):
        return ConstraintCreator.create_constraint(operation.get(CONSTRAINT))

    def update_auth_constraint(self, auth_key: str, constraint):
        self.state.set(config.make_state_path_for_auth_rule(auth_key),
                       self.constraint_serializer.serialize(constraint))

    def updateStateWithSingleTxn(self, txn, isCommitted=False):
        super().updateStateWithSingleTxn(txn, isCommitted)

        typ = get_type(txn)
        if typ == AUTH_RULE:
            self._update_auth_rule_state(get_payload_data(txn))
        elif typ == AUTH_RULES:
            payload = get_payload_data(txn)
            for rule in payload.get(RULES):
                self._update_auth_rule_state(rule)

    def _update_auth_rule_state(self, payload):
        constraint = self.get_auth_constraint(payload)
        auth_key = self.get_auth_key(payload)
        self.update_auth_constraint(auth_key, constraint)

    def handle_get_auth_rule(self, request: Request):
        proof = None
        operation = request.operation
        if len(operation) >= len(ClientGetAuthRuleOperation.schema) - 1:
            key = self.get_auth_key(operation)
            data, proof = self._get_auth_rule(key)
        else:
            data = self._get_all_auth_rules()
        result = self.make_result(request=request,
                                  data=data,
                                  proof=proof)
        result.update(request.operation)
        return result

    def _get_auth_rule(self, key):
        multi_sig = None
        if self._bls_store:
            root_hash = self.state.committedHeadHash
            encoded_root_hash = state_roots_serializer.serialize(bytes(root_hash))
            multi_sig = self._bls_store.get(encoded_root_hash)
        path = config.make_state_path_for_auth_rule(key)
        map_data, proof = self.get_value_from_state(path, with_proof=True, multi_sig=multi_sig)

        if map_data:
            data = self.constraint_serializer.deserialize(map_data)
        else:
            data = self.write_req_validator.auth_map[key]
        action_obj = split_action_id(key)
        return [self.make_get_auth_rule_result(data, action_obj)], proof

    def _get_all_auth_rules(self):
        data = self.write_req_validator.auth_map.copy()
        result = []
        for key in self.write_req_validator.auth_map:
            path = config.make_state_path_for_auth_rule(key)
            state_constraint, _ = self.get_value_from_state(path)
            if state_constraint:
                value = self.constraint_serializer.deserialize(state_constraint)
            else:
                value = data[key]
            action_obj = split_action_id(key)
            result.append(self.make_get_auth_rule_result(value, action_obj))
        return result

    def _check_auth_key(self, operation, identifier, req_id):
        auth_key = self.get_auth_key(operation)
        if auth_key not in self.write_req_validator.auth_map:
            raise InvalidClientRequest(identifier, req_id,
                                       "Unknown authorization rule: key '{}' is not "
                                       "found in authorization map.".format(auth_key))

    @staticmethod
    def make_get_auth_rule_result(constraint, action_obj):
        result = {CONSTRAINT: constraint.as_dict,
                  AUTH_TYPE: action_obj.txn_type,
                  AUTH_ACTION: action_obj.prefix,
                  FIELD: action_obj.field,
                  NEW_VALUE: action_obj.new_value,
                  }
        if action_obj.prefix == EDIT_PREFIX:
            result[OLD_VALUE] = action_obj.old_value
        return result
