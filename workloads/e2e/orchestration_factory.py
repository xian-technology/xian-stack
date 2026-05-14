import submission

last_prefix = Variable()
deployed_children = Hash(default_value="")
last_failure_prefix = Variable()

CHILD_TEMPLATE_MODULE = "__ORCH_TEMPLATE__"
CHILD_SOURCE = __ORCH_CHILD_SOURCE_JSON__
CHILD_VM_IR_TEMPLATE = __ORCH_CHILD_VM_IR_TEMPLATE_JSON__
CHILD_ARTIFACT_FORMAT = __ORCH_CHILD_ARTIFACT_FORMAT_JSON__
CHILD_VM_PROFILE = __ORCH_CHILD_VM_PROFILE_JSON__
CHILD_SOURCE_SHA256 = __ORCH_CHILD_SOURCE_SHA256_JSON__


def materialize_child_artifact_value(value: str, contract_name: str):
    return value.replace(CHILD_TEMPLATE_MODULE, contract_name)


def build_named_deployment_artifacts(contract_name: str):
    vm_ir_template = CHILD_VM_IR_TEMPLATE
    vm_ir_json = materialize_child_artifact_value(vm_ir_template, contract_name)
    return {
        "format": CHILD_ARTIFACT_FORMAT,
        "module_name": contract_name,
        "vm_profile": CHILD_VM_PROFILE,
        "source": CHILD_SOURCE,
        "vm_ir_json": vm_ir_json,
        "hashes": {
            "input_source_sha256": hashlib.sha256(CHILD_SOURCE),
            "source_sha256": CHILD_SOURCE_SHA256,
            "vm_ir_sha256": hashlib.sha256(vm_ir_json),
        },
    }


def remember(prefix: str, first: str, second: str):
    last_prefix.set(prefix)
    deployed_children[prefix, "first"] = first
    deployed_children[prefix, "second"] = second


@export
def deploy_family(prefix: str):
    assert prefix.startswith("con_"), "prefix must start with con_."
    first = prefix + "_alpha"
    second = prefix + "_beta"
    submission.submit_contract(
        name=first,
        deployment_artifacts=build_named_deployment_artifacts(first),
        constructor_args={"factory_name": ctx.this, "role": "alpha"},
    )
    submission.submit_contract(
        name=second,
        deployment_artifacts=build_named_deployment_artifacts(second),
        constructor_args={"factory_name": ctx.this, "role": "beta"},
    )
    remember(prefix, first, second)
    return {
        "factory": ctx.this,
        "caller": ctx.caller,
        "signer": ctx.signer,
        "children": [first, second],
    }


@export
def deploy_family_with_failure(prefix: str):
    assert prefix.startswith("con_"), "prefix must start with con_."
    first = prefix + "_good"
    second = prefix + "_bad"
    last_failure_prefix.set(prefix)
    submission.submit_contract(
        name=first,
        deployment_artifacts=build_named_deployment_artifacts(first),
        constructor_args={"factory_name": ctx.this, "role": "good"},
    )
    submission.submit_contract(
        name=second,
        deployment_artifacts=build_named_deployment_artifacts(second),
        constructor_args={
            "factory_name": ctx.this,
            "role": "bad",
            "should_fail": True,
        },
    )


@export
def get_last_family(prefix: str):
    return {
        "prefix": prefix,
        "first": deployed_children[prefix, "first"],
        "second": deployed_children[prefix, "second"],
    }
