# AiiDA imports.
from aiida import orm
from aiida.common import AttributeDict
from aiida.engine import ToContext, WorkChain, if_
from aiida.plugins import DataFactory

# AiiDA Quantum ESPRESSO plugin inputs.
from aiida_quantumespresso.common.types import ElectronicType, RelaxType, SpinType
from aiida_quantumespresso.utils.mapping import prepare_process_inputs
from aiida_quantumespresso.workflows.pw.relax import PwRelaxWorkChain

XyData = DataFactory("core.array.xy")
StructureData = DataFactory("core.structure")
BandsData = DataFactory("core.array.bands")
Orbital = DataFactory("core.orbital")


# functions to get entry points is copied from aiidalab_qe.app.utils
# because we want to decouple the workflows from the app, so I copied it here
# instead of importing it.
# load entry points
def get_entries(entry_point_name="aiidalab_qe.property"):
    from importlib.metadata import entry_points

    entries = {}
    for entry_point in entry_points().get(entry_point_name, []):
        entries[entry_point.name] = entry_point.load()

    return entries


# load entry point items
def get_entry_items(entry_point_name, item_name="workchain"):
    entries = get_entries(entry_point_name)
    return {
        name: entry_point.get(item_name)
        for name, entry_point in entries.items()
        if entry_point.get(item_name, False)
    }


plugin_entries = get_entry_items("aiidalab_qe.properties", "workchain")


class QeAppWorkChain(WorkChain):
    """WorkChain designed to calculate the requested properties in the AiiDAlab Quantum ESPRESSO app."""

    @classmethod
    def define(cls, spec):
        """Define the process specification."""
        # yapf: disable
        super().define(spec)
        spec.input('structure', valid_type=StructureData,
                   help='The inputs structure.')
        spec.input('clean_workdir', valid_type=orm.Bool, default=lambda: orm.Bool(True),
                   help='If `True`, work directories of all called calculation will be cleaned at the end of execution.')
        spec.input('properties', valid_type=orm.List, default=lambda: orm.List(),
                   help='The properties to calculate, used to control the logic of QeAppWorkChain.')
        spec.expose_inputs(PwRelaxWorkChain, namespace='relax', exclude=('clean_workdir', 'structure'),
                           namespace_options={'required': False, 'populate_defaults': False,
                                              'help': 'Inputs for the `PwRelaxWorkChain`, if not specified at all, the relaxation step is skipped.'})
        i = 0
        for name, entry_point in plugin_entries.items():
            plugin_workchain = entry_point["workchain"]
            spec.expose_inputs(
                plugin_workchain,
                namespace=name,
                exclude=entry_point.get("exclude"),
                namespace_options={
                    "required": False,
                    "populate_defaults": False,
                    "help": f"Inputs for the {name} plugin.",
                },
            )
            spec.expose_outputs(plugin_workchain,
                                namespace=name,
                                namespace_options={"required": False},
                                )
            spec.exit_code(
                403 + i,
                f"ERROR_SUB_PROCESS_FAILED_{name}",
                message=f"The plugin {name} WorkChain sub process failed",
            )
            i += 1
        spec.outline(
            cls.setup,
            if_(cls.should_run_relax)(
                cls.run_relax,
                cls.inspect_relax
            ),
            cls.run_plugin,
            cls.inspect_plugin,
            cls.results
        )
        spec.exit_code(401, 'ERROR_SUB_PROCESS_FAILED_RELAX',
                       message='The PwRelaxWorkChain sub process failed')
        spec.exit_code(402, 'ERROR_SUB_PROCESS_FAILED_PDOS',
                       message='The PdosWorkChain sub process failed')
        spec.output('structure', valid_type=StructureData, required=False)
        spec.output('band_parameters', valid_type=orm.Dict, required=False)
        spec.output('band_structure', valid_type=BandsData, required=False)
        spec.output('nscf_parameters', valid_type=orm.Dict, required=False)
        spec.output('dos', valid_type=XyData, required=False)
        spec.output('projections', valid_type=Orbital, required=False)
        spec.output('projections_up', valid_type=Orbital, required=False)
        spec.output('projections_down', valid_type=Orbital, required=False)
        # yapf: enable

    @classmethod
    def get_builder_from_protocol(
        cls,
        structure,
        parameters=None,
        **kwargs,
    ):
        """Return a builder prepopulated with inputs selected according to the chosen protocol."""
        import copy

        parameters = parameters or {}
        properties = parameters["workchain"].pop("properties", [])
        codes = parameters.pop("codes", {})
        codes = {key: orm.load_node(value) for key, value in codes.items()}
        # update pseudos
        for kind, uuid in parameters["advanced"]["pw"]["pseudos"].items():
            parameters["advanced"]["pw"]["pseudos"][kind] = orm.load_node(uuid)
        #
        builder = cls.get_builder()
        # Set the structure.
        builder.structure = structure
        # relax
        relax_overrides = {"base": parameters["advanced"]}
        protocol = parameters["workchain"]["protocol"]
        relax_builder = PwRelaxWorkChain.get_builder_from_protocol(
            code=codes.get("pw"),
            structure=structure,
            protocol=protocol,
            relax_type=RelaxType(parameters["workchain"]["relax_type"]),
            electronic_type=ElectronicType(parameters["workchain"]["electronic_type"]),
            spin_type=SpinType(parameters["workchain"]["spin_type"]),
            initial_magnetic_moments=parameters["advanced"]["initial_magnetic_moments"],
            overrides=relax_overrides,
            **kwargs,
        )
        # pop the inputs that are excluded from the expose_inputs
        relax_builder.pop("structure", None)
        relax_builder.pop("clean_workdir", None)
        relax_builder.pop("base_final_scf", None)  # never run a final scf
        builder.relax = relax_builder

        if properties is None:
            properties = []
        builder.properties = orm.List(list=properties)
        # add plugin workchain
        for name, entry_point in plugin_entries.items():
            if name in properties:
                plugin_builder = entry_point["get_builder"](
                    codes, structure, copy.deepcopy(parameters), **kwargs
                )
                setattr(builder, name, plugin_builder)
            else:
                builder.pop(name, None)

        # XXX (unkcpz) I smell not proper design here since I have to look at
        # configuration step to know what show be set here.
        clean_workdir = parameters["advanced"]["clean_workdir"]
        builder.clean_workdir = orm.Bool(clean_workdir)

        return builder

    def setup(self):
        """Perform the initial setup of the work chain, setup the input structure
        and the logic to determine which sub workchains to run.
        """
        self.ctx.current_structure = self.inputs.structure
        self.ctx.current_number_of_bands = None
        self.ctx.scf_parent_folder = None

        # logic based on the properties input
        self.ctx.run_relax = "relax" in self.inputs.properties
        self.ctx.run_pdos = "pdos" in self.inputs.properties

    def should_run_relax(self):
        """Check if the geometry of the input structure should be optimized."""
        return self.ctx.run_relax

    def run_relax(self):
        """Run the `PwRelaxWorkChain`."""
        inputs = AttributeDict(self.exposed_inputs(PwRelaxWorkChain, namespace="relax"))
        inputs.metadata.call_link_label = "relax"
        inputs.structure = self.ctx.current_structure

        inputs = prepare_process_inputs(PwRelaxWorkChain, inputs)
        running = self.submit(PwRelaxWorkChain, **inputs)

        self.report(f"launching PwRelaxWorkChain<{running.pk}>")

        return ToContext(workchain_relax=running)

    def inspect_relax(self):
        """Verify that the `PwRelaxWorkChain` finished successfully."""
        workchain = self.ctx.workchain_relax

        if not workchain.is_finished_ok:
            self.report(
                f"PwRelaxWorkChain failed with exit status {workchain.exit_status}"
            )
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_RELAX

        if "output_structure" in workchain.outputs:
            self.ctx.current_structure = workchain.outputs.output_structure
            self.ctx.current_number_of_bands = (
                workchain.outputs.output_parameters.get_attribute("number_of_bands")
            )
            self.out("structure", self.ctx.current_structure)

    def should_run_plugin(self, name):
        return name in self.inputs

    def run_plugin(self):
        """Run the plugin `WorkChain`."""
        plugin_running = {}
        for name, entry_point in plugin_entries.items():
            if not self.should_run_plugin(name):
                continue
            self.report(f"Run plugin : {name}")
            plugin_workchain = entry_point["workchain"]
            inputs = AttributeDict(
                self.exposed_inputs(plugin_workchain, namespace=name)
            )
            inputs.metadata.call_link_label = name
            inputs.structure = self.ctx.current_structure
            inputs = prepare_process_inputs(plugin_workchain, inputs)
            running = self.submit(plugin_workchain, **inputs)
            self.report(f"launching plugin {name} <{running.pk}>")
            plugin_running[name] = running

        return ToContext(**plugin_running)

    def inspect_plugin(self):
        """Verify that the `pluginWorkChain` finished successfully."""
        self.report("Inspect plugins:")
        for name, entry_point in plugin_entries.items():
            if not self.should_run_plugin(name):
                continue
            workchain = self.ctx[name]
            if not workchain.is_finished_ok:
                self.report(
                    f"Plugin {name} WorkChain failed with exit status {workchain.exit_status}"
                )
                return self.exit_codes.get(f"ERROR_SUB_PROCESS_FAILED_{name}")
            # Attach the output nodes directly as outputs of the workchain.
            self.out_many(
                self.exposed_outputs(
                    workchain, entry_point["workchain"], namespace=name
                )
            )

    def results(self):
        """Add the results to the outputs."""
        if "bands" in self.ctx:
            self.out("band_parameters", self.ctx.bands.outputs.band_parameters)
            self.out("band_structure", self.ctx.bands.outputs.band_structure)

        if "pdos" in self.ctx:
            self.out(
                "nscf_parameters",
                self.ctx.pdos.outputs.nscf.output_parameters,
            )
            self.out("dos", self.ctx.pdos.outputs.dos.output_dos)
            if "projections_up" in self.ctx.pdos.outputs.projwfc:
                self.out(
                    "projections_up",
                    self.ctx.pdos.outputs.projwfc.projections_up,
                )
                self.out(
                    "projections_down",
                    self.ctx.pdos.outputs.projwfc.projections_down,
                )
            else:
                self.out("projections", self.ctx.pdos.outputs.projwfc.projections)

    def on_terminated(self):
        """Clean the working directories of all child calculations if `clean_workdir=True` in the inputs."""
        super().on_terminated()

        if self.inputs.clean_workdir.value is False:
            self.report("remote folders will not be cleaned")
            return

        cleaned_calcs = []

        for called_descendant in self.node.called_descendants:
            if isinstance(called_descendant, orm.CalcJobNode):
                try:
                    called_descendant.outputs.remote_folder._clean()  # pylint: disable=protected-access
                    cleaned_calcs.append(called_descendant.pk)
                except (OSError, KeyError):
                    pass

        if cleaned_calcs:
            self.report(
                f"cleaned remote folders of calculations: {' '.join(map(str, cleaned_calcs))}"
            )
