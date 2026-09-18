def test_import_agent_kernel():
    import agent_kernel
    assert agent_kernel.__version__ == "0.1.0"

def test_import_core():
    from agent_kernel.core.runtime import Runtime
    assert Runtime is not None
