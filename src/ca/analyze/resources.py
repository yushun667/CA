"""Configurable OS resource-API annotation table.

The table says, for a called function name, whether the call acquires or
releases a resource, which resource family it belongs to, and where the
resource target lives (``return`` for allocators, ``arg0`` for lock/free
style APIs). Matching is by exact callee name; macro wrappers, function
pointers and indirect calls are not resolved here (documented limitation).

``options.resource_config`` accepts either a path to a JSON file or an inline
mapping/list:

* ``{"entries": [...]}`` -- replace the default table;
* ``{"extend": [...]}`` -- append to (and override) the default table;
* a JSON list -- shorthand for ``{"entries": [...]}``.

Each entry is ``{"api": "malloc", "op": "acquire", "resource_kind": "memory",
"target": "return", "releases": ["memory"], "matcher": "custom"}``. Later
entries win when the same API appears twice, so users can override the
default semantics (for example to treat a vendor ``my_close`` as a file
release).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

VALID_OPS = frozenset({"acquire", "release", "copy", "other"})
VALID_TARGETS = frozenset({"return", "arg0", "arg1", "none"})
_ARGUMENT_TARGETS = frozenset({"arg2", "arg3", "arg4", "arg5", "arg6", "arg7"})

# Later entries override earlier ones for the same API name.
_DEFAULT_ROWS: tuple[dict[str, Any], ...] = (
    # -- memory: C libc / POSIX ------------------------------------------
    {"api": "malloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "calloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "realloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "strdup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "strndup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "mmap", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "munmap", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "free", "op": "release", "resource_kind": "memory", "target": "arg0"},
    # -- memory: Linux kernel -------------------------------------------
    {"api": "kmalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kzalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kcalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "krealloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmalloc_array", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kcalloc_array", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kstrdup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kstrndup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kasprintf", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "vmalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "vzalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmem_cache_alloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "alloc_pages", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_kmalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_kzalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmemdup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmemdup_nul", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "memdup_user", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "memdup_user_nul", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "strndup_user", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmem_cache_alloc_node", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "get_zeroed_page", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "__get_free_page", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "__get_free_pages", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "free_page", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "dma_alloc_coherent", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "dma_free_coherent", "op": "release", "resource_kind": "memory", "target": "arg2"},
    {"api": "ioremap", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "iounmap", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "kvmalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kvcalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "mempool_alloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "bio_alloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "dma_pool_alloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "usb_alloc_urb", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kmem_cache_create", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "mempool_free", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "bio_put", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "dma_pool_free", "op": "release", "resource_kind": "memory", "target": "arg1"},
    {"api": "usb_free_urb", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "kmem_cache_destroy", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "get_page", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "put_page", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "pm_runtime_get_sync", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "pm_runtime_put", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "pm_runtime_put_sync", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "filp_open", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "filp_close", "op": "release", "resource_kind": "file", "target": "arg0"},
    {"api": "devm_kcalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_kstrdup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_kmemdup", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_ioremap", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "devm_iounmap", "op": "release", "resource_kind": "memory", "target": "arg1"},
    # xv6-riscv page allocator (returns one page, kfree frees a page).
    {"api": "kalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    # FreeRTOS heap_4/5 portable layer.
    {"api": "pvPortMalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "vPortFree", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "kfree", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "kvfree", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "vfree", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "kmem_cache_free", "op": "release", "resource_kind": "memory", "target": "arg1"},
    {"api": "devm_kfree", "op": "release", "resource_kind": "memory", "target": "arg1"},
    {"api": "__free_pages", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "free_pages", "op": "release", "resource_kind": "memory", "target": "arg0"},
    # -- files / file descriptors ---------------------------------------
    {"api": "open", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "openat", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "creat", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "fopen", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "fdopen", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "dup", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "dup2", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "filealloc", "op": "acquire", "resource_kind": "file", "target": "return"},
    {
        "api": "close",
        "op": "release",
        "resource_kind": "file",
        "target": "arg0",
        "releases": ["file", "socket"],
    },
    {"api": "fclose", "op": "release", "resource_kind": "file", "target": "arg0"},
    {"api": "fileclose", "op": "release", "resource_kind": "file", "target": "arg0"},
    # -- sockets ---------------------------------------------------------
    {"api": "socket", "op": "acquire", "resource_kind": "socket", "target": "return"},
    {"api": "accept", "op": "acquire", "resource_kind": "socket", "target": "return"},
    {"api": "socketpair", "op": "acquire", "resource_kind": "socket", "target": "return"},
    # -- locks -----------------------------------------------------------
    {"api": "spin_lock", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_lock_irq", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_lock_irqsave", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_lock_bh", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "mutex_lock", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "mutex_lock_interruptible", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "down", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "down_interruptible", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "down_read", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "down_write", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "acquire", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "ilock", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_unlock", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_unlock_irq", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_unlock_irqrestore", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "spin_unlock_bh", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "mutex_unlock", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "up", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "up_read", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "up_write", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "release", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "iunlock", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "iunlockput", "op": "release", "resource_kind": "lock", "target": "arg0"},
    # -- reference counts / kernel objects ------------------------------
    {"api": "refcount_inc", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "refcount_inc_not_zero", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "kref_get", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "get_task_struct", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "get_file", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "fget", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "fget_raw", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "fget_task", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "get_mm_exe_file", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "get_task_mm", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "get_task_pid", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "find_get_pid", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "allocate_mm", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "copy_process", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "get_pid_task", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "mmget", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "mmget_not_zero", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "anon_inode_getfile", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "anon_inode_getfile_secure", "op": "acquire", "resource_kind": "file", "target": "return"},
    {"api": "pidfd_prepare", "op": "acquire", "resource_kind": "file", "target": "arg2"},
    {"api": "__pidfd_prepare", "op": "acquire", "resource_kind": "file", "target": "arg2"},
    {"api": "try_module_get", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "bread", "op": "acquire", "resource_kind": "refcount", "target": "return"},
    {"api": "refcount_dec", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "refcount_dec_and_test", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "kref_put", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "put_task_struct", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "mmput", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "put_pid", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "free_pid", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "fput", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "module_put", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "brelse", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    # -- FreeRTOS objects (heap, queues, tasks, timers, event groups) -------
    {"api": "pvPortMalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "pvPortCalloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "pvPortRealloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "vPortFree", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "xQueueCreate", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "xQueueCreateMutex", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "xQueueGenericCreate", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "xQueueGenericCreateStatic", "op": "acquire", "resource_kind": "other", "target": "arg3"},
    {"api": "vQueueDelete", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "xSemaphoreCreateMutex", "op": "acquire", "resource_kind": "lock", "target": "return"},
    {"api": "xSemaphoreCreateBinary", "op": "acquire", "resource_kind": "lock", "target": "return"},
    {"api": "xSemaphoreCreateCounting", "op": "acquire", "resource_kind": "lock", "target": "return"},
    {"api": "vSemaphoreDelete", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "xSemaphoreTake", "op": "acquire", "resource_kind": "lock", "target": "arg0"},
    {"api": "xSemaphoreGive", "op": "release", "resource_kind": "lock", "target": "arg0"},
    {"api": "xTimerCreate", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "xTimerDelete", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "xEventGroupCreate", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "vEventGroupDelete", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "xStreamBufferCreate", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "vStreamBufferDelete", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "xTaskCreate", "op": "acquire", "resource_kind": "other", "target": "arg5"},
    {"api": "xTaskCreateStatic", "op": "acquire", "resource_kind": "other", "target": "arg5"},
    {"api": "vTaskDelete", "op": "release", "resource_kind": "other", "target": "arg0"},
    # -- RCU / preemption (no trackable object; recorded for coverage) -----
    {"api": "rcu_read_lock", "op": "acquire", "resource_kind": "lock", "target": "none"},
    {"api": "rcu_read_unlock", "op": "release", "resource_kind": "lock", "target": "none"},
    {"api": "rcu_read_lock_bh", "op": "acquire", "resource_kind": "lock", "target": "none"},
    {"api": "rcu_read_unlock_bh", "op": "release", "resource_kind": "lock", "target": "none"},
    {"api": "rcu_read_lock_sched", "op": "acquire", "resource_kind": "lock", "target": "none"},
    {"api": "rcu_read_unlock_sched", "op": "release", "resource_kind": "lock", "target": "none"},
    # -- network buffers -------------------------------------------------
    {"api": "alloc_skb", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "__alloc_skb", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "dev_alloc_skb", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "napi_alloc_skb", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "skb_clone", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "skb_copy", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "kfree_skb", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "consume_skb", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "dev_kfree_skb", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "__kfree_skb", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "sock_create", "op": "acquire", "resource_kind": "socket", "target": "return"},
    {"api": "sock_release", "op": "release", "resource_kind": "socket", "target": "arg0"},
    {"api": "sk_alloc", "op": "acquire", "resource_kind": "memory", "target": "return"},
    {"api": "sk_free", "op": "release", "resource_kind": "memory", "target": "arg0"},
    {"api": "alloc_netdev", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "free_netdev", "op": "release", "resource_kind": "other", "target": "arg0"},
    # -- device / of-node reference counts -------------------------------
    {"api": "get_device", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "put_device", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "of_node_get", "op": "acquire", "resource_kind": "refcount", "target": "arg0"},
    {"api": "of_node_put", "op": "release", "resource_kind": "refcount", "target": "arg0"},
    {"api": "regulator_get", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "devm_regulator_get", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "regulator_enable", "op": "acquire", "resource_kind": "other", "target": "arg0"},
    {"api": "regulator_disable", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "regulator_put", "op": "release", "resource_kind": "other", "target": "arg0"},
    # -- IRQs ------------------------------------------------------------
    {"api": "request_irq", "op": "acquire", "resource_kind": "irq", "target": "arg0"},
    {"api": "request_threaded_irq", "op": "acquire", "resource_kind": "irq", "target": "arg0"},
    {"api": "request_any_context_irq", "op": "acquire", "resource_kind": "irq", "target": "arg0"},
    {"api": "devm_request_irq", "op": "acquire", "resource_kind": "irq", "target": "arg1"},
    {"api": "devm_request_threaded_irq", "op": "acquire", "resource_kind": "irq", "target": "arg1"},
    {"api": "devm_free_irq", "op": "release", "resource_kind": "irq", "target": "arg1"},
    {"api": "disable_irq_nosync", "op": "release", "resource_kind": "irq", "target": "arg0"},
    {"api": "enable_irq", "op": "acquire", "resource_kind": "irq", "target": "arg0"},
    {"api": "free_irq", "op": "release", "resource_kind": "irq", "target": "arg0"},
    {"api": "disable_irq", "op": "release", "resource_kind": "irq", "target": "arg0"},
    # -- clocks / PCI / misc --------------------------------------------
    {"api": "clk_get", "op": "acquire", "resource_kind": "other", "target": "return"},
    {"api": "clk_prepare_enable", "op": "acquire", "resource_kind": "other", "target": "arg0"},
    {"api": "pci_enable_device", "op": "acquire", "resource_kind": "other", "target": "arg0"},
    {"api": "pci_alloc_irq_vectors", "op": "acquire", "resource_kind": "irq", "target": "arg0"},
    {"api": "pci_request_regions", "op": "acquire", "resource_kind": "other", "target": "arg0"},
    {"api": "pci_release_regions", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "pci_free_irq_vectors", "op": "release", "resource_kind": "irq", "target": "arg0"},
    {"api": "begin_op", "op": "acquire", "resource_kind": "other", "target": "arg0"},
    {"api": "clk_put", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "clk_disable_unprepare", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "pci_disable_device", "op": "release", "resource_kind": "other", "target": "arg0"},
    {"api": "end_op", "op": "release", "resource_kind": "other", "target": "arg0"},
)


@dataclass(frozen=True)
class ResourceApi:
    """One row of the annotation table."""

    api: str
    op: str
    resource_kind: str
    target: str = "arg0"
    releases: tuple[str, ...] = ()
    matcher: str = ""

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ValueError(f"invalid resource op {self.op!r} for api {self.api!r}")
        if self.target not in VALID_TARGETS and self.target not in _ARGUMENT_TARGETS:
            raise ValueError(f"invalid resource target {self.target!r} for api {self.api!r}")
        if not self.releases:
            object.__setattr__(self, "releases", (self.resource_kind,))
        if not self.matcher:
            object.__setattr__(self, "matcher", f"default:{self.api}")


_DEFAULT_CONFIG: "ResourceConfig | None" = None
_CONFIG_FILE_CACHE: dict[str, "ResourceConfig"] = {}


class ResourceConfig:
    """Ordered annotation table with deterministic exact-name matching."""

    def __init__(self, entries: Iterable[ResourceApi]) -> None:
        self._entries: tuple[ResourceApi, ...] = tuple(entries)
        self._by_api: dict[str, ResourceApi] = {}
        for entry in self._entries:
            self._by_api[entry.api] = entry

    # -- construction ----------------------------------------------------
    @classmethod
    def default(cls) -> "ResourceConfig":
        global _DEFAULT_CONFIG
        if _DEFAULT_CONFIG is None:
            _DEFAULT_CONFIG = cls.from_rows(_DEFAULT_ROWS, origin="default")
        return _DEFAULT_CONFIG

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]], origin: str) -> "ResourceConfig":
        entries: list[ResourceApi] = []
        for row in rows:
            entries.append(_row_to_api(row, origin))
        return cls(entries)

    @classmethod
    def from_options(cls, value: Any = None) -> "ResourceConfig":
        if value is None:
            return cls.default()
        if isinstance(value, (str, os.PathLike)):
            key = os.fspath(value)
            cached = _CONFIG_FILE_CACHE.get(key)
            if cached is not None:
                return cached
            config = cls._from_config_data(_load_config_data(key))
            _CONFIG_FILE_CACHE[key] = config
            return config
        return cls._from_config_data(value)

    @classmethod
    def _from_config_data(cls, data: Any) -> "ResourceConfig":
        if isinstance(data, list):
            return cls.from_rows(data, origin="config")
        if not isinstance(data, dict):
            raise ValueError("resource_config must be a path, mapping, or list")
        base = cls.default()
        if "entries" in data:
            return cls.from_rows(data["entries"], origin="config")
        if "extend" in data:
            extra = [_row_to_api(row, "config") for row in data["extend"]]
            merged = list(base._entries) + extra
            return cls(merged)
        raise ValueError("resource_config mapping needs 'entries' or 'extend'")

    # -- queries ---------------------------------------------------------
    def match(self, api_name: str) -> ResourceApi | None:
        return self._by_api.get(api_name) if api_name else None

    def entries(self) -> tuple[ResourceApi, ...]:
        return self._entries

    def release_kinds(self) -> frozenset[str]:
        kinds: set[str] = set()
        for entry in self._entries:
            if entry.op == "release":
                kinds.update(entry.releases)
        return frozenset(kinds)

    def has_release_for(self, resource_kind: str) -> bool:
        return resource_kind in self.release_kinds()


def _row_to_api(row: Any, origin: str) -> ResourceApi:
    if not isinstance(row, dict):
        raise ValueError(f"resource entry must be a mapping, got {type(row)!r}")
    api = str(row.get("api", "")).strip()
    if not api:
        raise ValueError("resource entry needs a non-empty 'api'")
    matcher = str(row.get("matcher") or f"{origin}:{api}")
    raw_releases = row.get("releases") or ()
    if isinstance(raw_releases, str):
        raw_releases = (raw_releases,)
    return ResourceApi(
        api=api,
        op=str(row.get("op", "other")),
        resource_kind=str(row.get("resource_kind", "other")),
        target=str(row.get("target", "arg0")),
        releases=tuple(str(item) for item in raw_releases),
        matcher=matcher,
    )


def _load_config_data(value: Any) -> Any:
    if isinstance(value, (str, os.PathLike)):
        path = os.fspath(value)
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return value


__all__ = [
    "VALID_OPS",
    "VALID_TARGETS",
    "ResourceApi",
    "ResourceConfig",
]
