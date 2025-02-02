load("@rules_cc//cc:defs.bzl", "cc_library")
load("@bazel_skylib//lib:paths.bzl", "paths")

CPU_CAPABILITY_NAMES = ["DEFAULT", "AVX2"]
CAPABILITY_COMPILER_FLAGS = {
    "AVX2": ["-mavx2", "-mfma"],
    "DEFAULT": [],
}

PREFIX = "aten/src/ATen/native/"

def intern_build_aten_ops(copts, deps):
    for cpu_capability in CPU_CAPABILITY_NAMES:
        srcs = []
        for impl in native.glob(
            [
                PREFIX + "cpu/*.cpp",
                PREFIX + "quantized/cpu/kernels/*.cpp",
            ],
        ):
            name = impl.replace(PREFIX, "")
            out = PREFIX + name + "." + cpu_capability + ".cpp"
            native.genrule(
                name = name + "_" + cpu_capability + "_cp",
                srcs = [impl],
                outs = [out],
                cmd = "cp $< $@",
            )
            srcs.append(out)

        cc_library(
            name = "ATen_CPU_" + cpu_capability,
            srcs = srcs,
            copts = copts + [
                "-DCPU_CAPABILITY=" + cpu_capability,
                "-DCPU_CAPABILITY_" + cpu_capability,
            ] + CAPABILITY_COMPILER_FLAGS[cpu_capability],
            deps = deps,
            linkstatic = 1,
        )
    cc_library(
        name = "ATen_CPU",
        deps = [":ATen_CPU_" + cpu_capability for cpu_capability in CPU_CAPABILITY_NAMES],
        linkstatic = 1,
    )

def generate_aten_impl(ctx):
    # Declare the entire ATen/ops/ directory as an output
    ops_dir = ctx.actions.declare_directory("aten/src/ATen/ops")
    outputs=[ops_dir] + ctx.outputs.outs

    install_dir = paths.dirname(ops_dir.path)
    tool_inputs, tool_inputs_manifest = ctx.resolve_tools(tools=[ctx.attr.generator])
    ctx.actions.run_shell(
        outputs=outputs,
        inputs=ctx.files.srcs,
        command=ctx.executable.generator.path + " $@",
        arguments=["--source-path", "aten/src/ATen",
                   "--per-operator-headers",
                   "--install_dir", install_dir],
        tools=tool_inputs,
        input_manifests=tool_inputs_manifest,
        use_default_shell_env=True,
    )
    return [DefaultInfo(files=depset(outputs))]


generate_aten = rule(
    implementation = generate_aten_impl,
    attrs = {
        "outs": attr.output_list(),
        "srcs": attr.label_list(allow_files=True),
        "generator": attr.label(
            executable=True,
            allow_files=True,
            mandatory=True,
            cfg="exec",
        ),
    }
)
