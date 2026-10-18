"""Linux-kernel resource annotation and precision tests for ca.analyze."""

from __future__ import annotations

from ca.analyze import ResourceConfig
from ca.core.schema import record_to_dict
try:  # pragma: no cover - import layout differs when tests is a package
    from test_analyze_common import unit_for_fixture
except ImportError:  # pragma: no cover - e.g. server deployment with tests/__init__.py
    from tests.test_analyze_common import unit_for_fixture

# APIs required for the Linux-kernel subset, with expected annotation shape.
REQUIRED_APIS = {
    "kmalloc": ("acquire", "memory"),
    "kzalloc": ("acquire", "memory"),
    "kcalloc": ("acquire", "memory"),
    "krealloc": ("acquire", "memory"),
    "kfree": ("release", "memory"),
    "vmalloc": ("acquire", "memory"),
    "vfree": ("release", "memory"),
    "kvfree": ("release", "memory"),
    "kmem_cache_alloc": ("acquire", "memory"),
    "kmem_cache_free": ("release", "memory"),
    "devm_kzalloc": ("acquire", "memory"),
    "devm_kfree": ("release", "memory"),
    "spin_lock": ("acquire", "lock"),
    "spin_unlock": ("release", "lock"),
    "spin_lock_irqsave": ("acquire", "lock"),
    "spin_unlock_irqrestore": ("release", "lock"),
    "mutex_lock": ("acquire", "lock"),
    "mutex_unlock": ("release", "lock"),
    "down": ("acquire", "lock"),
    "up": ("release", "lock"),
    "rcu_read_lock": ("acquire", "lock"),
    "rcu_read_unlock": ("release", "lock"),
    "refcount_inc": ("acquire", "refcount"),
    "refcount_dec": ("release", "refcount"),
    "kref_get": ("acquire", "refcount"),
    "kref_put": ("release", "refcount"),
    "get_task_struct": ("acquire", "refcount"),
    "put_task_struct": ("release", "refcount"),
    "clk_get": ("acquire", "other"),
    "clk_put": ("release", "other"),
    "pci_enable_device": ("acquire", "other"),
    "pci_disable_device": ("release", "other"),
    "alloc_skb": ("acquire", "memory"),
    "kfree_skb": ("release", "memory"),
    "consume_skb": ("release", "memory"),
    "request_irq": ("acquire", "irq"),
    "free_irq": ("release", "irq"),
}


def _analyze(function_name: str):
    return unit_for_fixture("df_kernel_resources.c").analyze(function_name)


def _findings(result, kind: str | None = None):
    if kind is None:
        return list(result.findings)
    return [item for item in result.findings if item.kind == kind]


def test_default_table_covers_kernel_apis():
    config = ResourceConfig.default()
    for api, (op, kind) in REQUIRED_APIS.items():
        entry = config.match(api)
        assert entry is not None, api
        assert (entry.op, entry.resource_kind) == (op, kind), api
        assert entry.matcher == f"default:{api}"
    # Out-parameter APIs: the handle lives in arg1 / arg5, not arg0.
    assert config.match("kmem_cache_free").target == "arg1"
    assert config.match("devm_kfree").target == "arg1"
    assert config.match("xTaskCreate").target == "arg5"
    assert config.match("rcu_read_lock").target == "none"


def test_slab_roundtrip_and_leak():
    balanced = _analyze("cache_roundtrip")
    assert [op.api_name for op in balanced.resource_ops] == [
        "kmem_cache_alloc",
        "kmem_cache_free",
        "kmem_cache_free",
    ]
    assert [op.target for op in balanced.resource_ops] == ["obj", "obj", "obj"]
    assert _findings(balanced) == []

    leak = _analyze("cache_leak")
    leaks = _findings(leak, "leak")
    assert len(leaks) == 1
    assert leaks[0].resource_kind == "memory"


def test_is_err_error_pointer_is_not_a_leak():
    result = _analyze("clk_error_path")
    assert [op.api_name for op in result.resource_ops] == ["clk_get", "clk_put"]
    assert _findings(result) == []


def test_goto_cleanup_label_balances_every_path():
    result = _analyze("goto_cleanup")
    api_names = [op.api_name for op in result.resource_ops]
    assert api_names == ["kmalloc", "kmalloc", "kfree", "kfree"]
    assert _findings(result) == []


def test_skb_roundtrip_and_leak():
    balanced = _analyze("skb_roundtrip")
    assert [op.api_name for op in balanced.resource_ops] == [
        "alloc_skb",
        "kfree_skb",
        "consume_skb",
    ]
    assert _findings(balanced) == []

    leak = _analyze("skb_leak")
    leaks = _findings(leak, "leak")
    assert len(leaks) == 1
    assert "alloc_skb" in leaks[0].message


def test_ownership_transfer_patterns_are_not_leaks():
    for function_name in (
        "devm_owned",
        "return_owned",
        "store_owned",
        "drvdata_owned",
        "list_owned",
    ):
        result = _analyze(function_name)
        assert _findings(result) == [], function_name


def test_rcu_section_is_recorded_without_findings():
    result = _analyze("rcu_section")
    assert [(op.op, op.api_name, op.target) for op in result.resource_ops] == [
        ("acquire", "rcu_read_lock", ""),
        ("release", "rcu_read_unlock", ""),
    ]
    assert _findings(result) == []


def test_lock_irqsave_pair_and_irq_failure_path():
    locked = _analyze("lock_irqsave_pair")
    assert [op.api_name for op in locked.resource_ops] == [
        "mutex_lock",
        "spin_lock_irqsave",
        "spin_unlock_irqrestore",
        "mutex_unlock",
    ]
    assert _findings(locked) == []

    paired = _analyze("irq_pair")
    assert [op.api_name for op in paired.resource_ops] == ["request_irq", "free_irq"]
    assert _findings(paired) == []  # failure branch (ret != 0) has no resource

    leak = _analyze("irq_leak")
    leaks = _findings(leak, "leak")
    assert len(leaks) == 1
    assert leaks[0].resource_kind == "irq"


def test_kernel_fixture_is_deterministic():
    unit = unit_for_fixture("df_kernel_resources.c")
    for function in unit.functions:
        first = unit.analyze(function.name)
        second = unit.analyze(function.name)
        assert [record_to_dict(item) for item in first.resource_ops] == [
            record_to_dict(item) for item in second.resource_ops
        ]
        assert [record_to_dict(item) for item in first.findings] == [
            record_to_dict(item) for item in second.findings
        ]
