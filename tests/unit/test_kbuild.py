from genut.backends.kunit.kbuild import effective_kind, file_is_built, registration_of


def _tree(tmp_path, makefile):
    d = tmp_path / "drivers" / "foo"
    d.mkdir(parents=True)
    (d / "Makefile").write_text(makefile, encoding="utf-8")
    return tmp_path


def test_conditional_registration(tmp_path):
    k = _tree(tmp_path, "obj-$(CONFIG_FOO) += foo.o\nobj-$(CONFIG_FOO_KUNIT_TEST) += \\\n\tfoo_kunit.o\n")
    reg = registration_of(k, "drivers/foo/foo_kunit.c")
    assert reg.kind == "conditional" and reg.config_symbol == "CONFIG_FOO_KUNIT_TEST"
    assert effective_kind(reg, "CONFIG_FOO_KUNIT_TEST=y\n") == "built-in"
    assert effective_kind(reg, "CONFIG_FOO_KUNIT_TEST=m\n") == "module"
    assert effective_kind(reg, "# CONFIG_FOO_KUNIT_TEST is not set\n") == "off"


def test_plain_and_composite(tmp_path):
    k = _tree(tmp_path, "obj-y += a.o\nobj-m += bar.o\nbar-objs := x.o foo_kunit.o\n")
    assert registration_of(k, "drivers/foo/a.c").kind == "built-in"
    reg = registration_of(k, "drivers/foo/foo_kunit.c")
    assert reg.kind == "module" and reg.object_name == "bar.o"
    assert registration_of(k, "drivers/foo/missing.c") is None


def test_file_is_built(tmp_path):
    k = _tree(tmp_path, "obj-$(CONFIG_FOO) += foo.o\n")
    src = k / "drivers" / "foo" / "foo.c"
    assert not file_is_built(k, src, "")
    assert file_is_built(k, src, "CONFIG_FOO=y\n")
