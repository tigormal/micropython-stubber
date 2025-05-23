# sourcery skip: snake-case-functions
"""
Merge documentation and type information 
- from an doctring-rich and typed stub module
- infor a less well documented and typed stub module 
"""
# Copyright Jos Verlinde
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#

import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypeVar, Union, cast

import libcst as cst
import libcst.matchers as m
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor, GatherImportsVisitor, ImportItem
from libcst.helpers.module import insert_header_comments

from mpflash.logger import log
from stubber.typing_collector import (
    MODULE_KEY,
    AnnoValue,
    StubTypingCollector,
    is_deleter,
    is_getter,
    is_property,
    is_setter,
    update_def_docstr,
    update_module_docstr,
)

from .visitors.type_helpers import AddTypeHelpers, GatherTypeHelpers

Mod_Class_T = TypeVar("Mod_Class_T", cst.Module, cst.ClassDef)
"""TypeVar for Module or ClassDef that both support overloads"""
##########################################################################################
# # log = logging.getLogger(__name__)
#########################################################################################
empty_module = cst.parse_module("")  # Debugging aid : empty_module.code_for_node(node)
_code = empty_module.code_for_node


def is_decorator(dec: cst.CSTNode, name: str) -> bool:
    """shorthand to determin if something is a specific decorator"""
    return m.matches(dec, m.Decorator(decorator=m.Name(value=name)))


class MergeCommand(VisitorBasedCodemodCommand):
    """
    A libcst transformer that merges the type-rich information from a doc-stub into
    a firmware stub.
    The resulting file will contain information from both sources.

    - module docstring - from source

    - function parameters and types - from docstubs
    - function return types - from docstubs
    - function docstrings - from source

    """

    DESCRIPTION: str = "Merge the type-rich information from a doc-stub into a firmware stub"
    copy_params: bool = True
    copy_docstr: bool = True

    @staticmethod
    def add_args(arg_parser: argparse.ArgumentParser) -> None:
        """Add command-line args that a user can specify for running this codemod."""

        arg_parser.add_argument(
            "--stubfile",
            dest="docstub_file",
            metavar="PATH",
            help="The path to the doc-stub file",
            type=str,
            required=True,
        )

        arg_parser.add_argument(
            "--copy-params",
            dest="copy_params",
            default=False,
        )

        arg_parser.add_argument(
            "--copy-docstr",
            dest="copy_docstr",
            default=True,
        )

    def __init__(
        self,
        context: CodemodContext,
        docstub_file: Union[Path, str],
        copy_params: bool = False,
        copy_docstr: bool = True,
    ) -> None:
        """initialize the base class with context, and save our args."""
        super().__init__(context)
        self.replace_functiondef_with_classdef = True
        # stack for storing the canonical name of the current function/method
        self.stack: List[str] = []
        # stubfile is the path to the doc-stub file
        self.docstub_path = Path(docstub_file)
        # read the stub file from the path
        self.docstub_source = self.docstub_path.read_text(encoding="utf-8")
        # store the annotations
        self.annotations: Dict[
            Tuple[str, ...],  # key: tuple of canonical class/function name
            AnnoValue,
            # Union[TypeInfo, str, List[TypeInfo]],  # value: TypeInfo
        ] = {}
        self.comments: List[str] = []

        self.copy_params = copy_params
        self.copy_docstr = copy_docstr

        self.stub_imports: Dict[str, ImportItem] = {}
        self.all_imports: List[Union[cst.Import, cst.ImportFrom]] = []
        self.type_helpers = {}
        # parse the doc-stub file
        if self.docstub_source:
            try:
                # parse the doc-stub file
                stub_tree = cst.parse_module(self.docstub_source)
            except cst.ParserSyntaxError as e:
                log.error(f"Error parsing {self.docstub_path}: {e}")
                raise ValueError(f"Error parsing {self.docstub_path}: {e}") from e
            # create the collectors
            typing_collector = StubTypingCollector()
            import_collector = GatherImportsVisitor(context)
            typevar_collector = GatherTypeHelpers(context)
            # visit the source / doc-stub file with all collectors
            stub_tree.visit(typing_collector)
            self.annotations = typing_collector.annotations
            self.comments = typing_collector.comments
            # Store the imports that were added to the source / doc-stub file
            stub_tree.visit(import_collector)
            self.stub_imports = import_collector.symbol_mapping
            self.all_imports = import_collector.all_imports
            # Get typevars, type aliasses and ParamSpecs
            stub_tree.visit(typevar_collector)
            self.type_helpers = typevar_collector.all_typehelpers
            pass

    # ------------------------------------------------------------------------

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        """
        This method is responsible for updating the module node after processing it in the codemod.
        It performs the following tasks:
        1. Adds any needed imports from the doc-stub.
        2. Adds `from module import *` from the doc-stub.
        3. Updates the module docstring.
        4. Updates the comments in the module.

        :param original_node: The original module node.
        :param updated_node: The updated module node after processing.
        :return: The updated module node.
        """
        # --------------------------------------------------------------------
        # add any needed imports from the source doc-stub
        for k in self.stub_imports.keys():
            import_item = self.stub_imports[k]
            if import_item.module_name == self.context.full_module_name:
                # this is an import from the same module we should NOT add it
                continue
            if import_item.module_name.split(".")[0] == self.context.full_module_name and not self.context.filename.endswith(
                "__init__.pyi"
            ):
                # this is an import from a module child module we should NOT add it
                continue
            log.trace(f"add: import {k} = {import_item}")
            AddImportsVisitor.add_needed_import(
                self.context,
                module=import_item.module_name,
                obj=import_item.obj_name,
                asname=import_item.alias,
                relative=import_item.relative,
            )

        # add `from module import *` from the doc-stub
        # FIXME: this cases a problem if there is also a 'from module import foobar' in the firmware stub
        # also all comments get removed from the import
        if self.all_imports:
            for import_item in self.all_imports:
                if isinstance(import_item, cst.ImportFrom):
                    # perhaps this is an import from *
                    if isinstance(import_item.names, cst.ImportStar):
                        # bit of a hack to get the full module name
                        empty_mod = cst.parse_module("")
                        full_module_name = empty_mod.code_for_node(import_item.module)  # type: ignore
                        log.trace(f"add: from {full_module_name} import *")
                        AddImportsVisitor.add_needed_import(
                            self.context,
                            module=full_module_name,
                            obj="*",
                        )
        # --------------------------------------------------------------------
        # Add any typevars to the module
        if self.type_helpers:
            AddTypeHelpers.add_helpers(self.context, self.type_helpers)
            atv = AddTypeHelpers(self.context)
            updated_node = atv.transform_module(updated_node)

        # --------------------------------------------------------------------
        # update the docstring.
        if MODULE_KEY in self.annotations:
            # update/replace  module docstrings
            # todo: or should we add / merge the docstrings?
            docstub_docstr = self.annotations[MODULE_KEY].docstring
            assert isinstance(docstub_docstr, str)
            src_docstr = original_node.get_docstring() or ""
            if src_docstr or docstub_docstr:
                if self.copy_docstr and (docstub_docstr.strip() != src_docstr.strip()):
                    if src_docstr and self.copy_docstr:
                        log.trace(f"Append module docstrings. (new --- old) ")
                        new_docstr = '"""\n' + docstub_docstr + "\n\n---\n" + src_docstr + '\n"""'
                    else:
                        new_docstr = '"""\n' + docstub_docstr + '\n"""'

                    docstr_node = cst.SimpleStatementLine(
                        body=[
                            cst.Expr(
                                value=cst.SimpleString(
                                    value=new_docstr,
                                )
                            )
                        ]
                    )
                    updated_node = update_module_docstr(updated_node, docstr_node)
        # --------------------------------------------------------------------
        # update the comments
        updated_node = insert_header_comments(updated_node, self.comments)

        # --------------------------------------------------------------------
        # make sure that any @overloads that not yet applied  are also added to the firmware stub
        updated_node = self.add_missed_overloads(updated_node, stack_id=())
        # Add any missing @mp_available
        updated_node = self.add_missed_mp_available(updated_node, stack_id=())
        return updated_node

    def add_missed_overloads(self, updated_node: Mod_Class_T, stack_id: tuple) -> Mod_Class_T:
        """
        Add any missing overloads to the updated_node

        """
        missing_overloads = []
        scope_keys = [k for k in self.annotations.keys() if k[: (len(stack_id))] == stack_id]

        for key in scope_keys:
            for overload in self.annotations[key].overloads:
                missing_overloads.append((overload.def_node, key))
            self.annotations[key].overloads = []  # remove for list, assume  works

        if missing_overloads:
            if isinstance(updated_node, cst.Module):
                module_level = True
                updated_body = list(updated_node.body)  # type: ignore
            elif isinstance(updated_node, cst.ClassDef):
                module_level = False
                updated_body = list(updated_node.body.body)  # type: ignore
            else:
                raise ValueError(f"Unsupported node type: {updated_node}")
            # insert each overload just after a function with the same name

            new_classes = []
            for overload, key in missing_overloads:
                matched = False
                matched, i = self.locate_function_by_name(overload, updated_body)
                if matched:
                    log.trace(f"Add @overload for {overload.name.value}")
                    if self.copy_params:
                        docstring_node = self.annotations[key].docstring_node or ""
                        # Use the new overload - but with the existing docstring
                        overload = update_def_docstr(overload, docstring_node)
                    updated_body.insert(i + 1, overload)
                else:
                    # add to the end of the module or  class
                    if module_level and len(key) > 1:
                        # this is a class level overload, for which no class was found
                        class_name = key[0]
                        if class_name not in new_classes:
                            new_classes.append(class_name)
                            # create a class for it, and then add all the overload methods to that class
                            log.trace(f"Add New class  @overload for {overload.name.value} at the end of the module")
                            # create a list of all overloads for this class
                            class_overloads = [overload for overload, k in missing_overloads if k[0] == class_name]
                            class_def = cst.ClassDef(
                                name=cst.Name(value=class_name),
                                body=cst.IndentedBlock(body=class_overloads),
                            )
                            updated_body.append(class_def)
                        else:
                            # already processed this class method
                            pass
                    else:
                        log.trace(f"Add @overload for {overload.name.value} at the end of the class")
                        updated_body.append(overload)

            if isinstance(updated_node, cst.Module):
                updated_node = updated_node.with_changes(body=tuple(updated_body))
            elif isinstance(updated_node, cst.ClassDef):
                b1 = updated_node.body.with_changes(body=tuple(updated_body))
                updated_node = updated_node.with_changes(body=b1)

                # cst.IndentedBlock(body=tuple(updated_body)))  # type: ignore
        return updated_node

    def add_missed_mp_available(self, updated_node: Mod_Class_T, stack_id: tuple) -> Mod_Class_T:
        """
        Add any missing @mp_available to the updated_node

        """
        missing_decorated = []
        scope_keys = [k for k in self.annotations.keys() if k[: (len(stack_id))] == stack_id]

        for key in scope_keys:
            for mpa in self.annotations[key].mp_available:
                missing_decorated.append((mpa.def_node, key))
            self.annotations[key].mp_available = []  # remove for list, assume  works

        if missing_decorated:
            if isinstance(updated_node, cst.Module):
                module_level = True
                updated_body = list(updated_node.body)  # type: ignore
            elif isinstance(updated_node, cst.ClassDef):
                module_level = False
                updated_body = list(updated_node.body.body)  # type: ignore
            else:
                raise ValueError(f"Unsupported node type: {updated_node}")
            # insert each decorated function/class just after a function with the same name

            new_classes = []
            for mpa, key in missing_decorated:
                matched = False
                matched, i = self.locate_function_by_name(mpa, updated_body)
                if matched:
                    log.trace(f"Add @mp_available for {mpa.name.value}")
                    if self.copy_params:
                        docstring_node = self.annotations[key].docstring_node or ""
                        # Use the new overload - but with the existing docstring
                        mpa = update_def_docstr(mpa, docstring_node)
                    updated_body.insert(i + 1, mpa)
                else:
                    # add to the end of the module or  class
                    if module_level and len(key) > 1:
                        # this is a class level @mp_available, for which no class was found
                        class_name = key[0]
                        if class_name not in new_classes:
                            new_classes.append(class_name)
                            # create a class for it, and then add all the @mp_available methods to that class
                            log.trace(f"Add New class @mp_available for {mpa.name.value} at the end of the module")
                            # create a list of all overloads for this class
                            class_mpas = [mpa for mpa, k in missing_decorated if k[0] == class_name]
                            class_def = cst.ClassDef(
                                name=cst.Name(value=class_name),
                                body=cst.IndentedBlock(body=class_mpas),
                            )
                            updated_body.append(class_def)
                        else:
                            # already processed this class method
                            pass
                    else:
                        log.trace(f"Add @mp_available for {mpa.name.value} at the end of the class")
                        updated_body.append(mpa)

            if isinstance(updated_node, cst.Module):
                updated_node = updated_node.with_changes(body=tuple(updated_body))
            elif isinstance(updated_node, cst.ClassDef):
                b1 = updated_node.body.with_changes(body=tuple(updated_body))
                updated_node = updated_node.with_changes(body=b1)

                # cst.IndentedBlock(body=tuple(updated_body)))  # type: ignore
        return updated_node

    def locate_function_by_name(self, overload, updated_body):
        """locate the (last) function by name"""
        matched = False
        for i, node in reversed(list(enumerate(updated_body))):
            if isinstance(node, cst.FunctionDef) and node.name.value == overload.name.value:
                matched = True
                break
        return matched, i
        # --------------------------------------------------------------------

    # ------------------------------------------------------------

    def visit_ClassDef(self, node: cst.ClassDef) -> Optional[bool]:
        """keep track of the the (class, method) names to the stack"""
        self.stack.append(node.name.value)

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        stack_id = tuple(self.stack)
        self.stack.pop()
        if stack_id not in self.annotations:
            # no changes to the class
            return updated_node
        # update the firmware_stub from the doc_stub information
        doc_stub = self.annotations[stack_id].type_info
        # first update the docstring
        updated_node = update_def_docstr(updated_node, doc_stub.docstr_node)
        # Sometimes the MCU stubs and the doc stubs have different types : FunctionDef / ClassDef
        # we need to be careful not to copy over all the annotations if the types are different
        if doc_stub.def_type == "classdef":
            # Same type, we can copy over all the annotations
            # combine the decorators from the doc-stub and the firmware stub
            new_decorators = []
            if doc_stub.decorators:
                new_decorators.extend(doc_stub.decorators)
            if updated_node.decorators:
                new_decorators.extend(updated_node.decorators)

            updated_node = updated_node.with_changes(
                decorators=new_decorators,
                bases=doc_stub.def_node.bases,  # type: ignore
            )
        # else:
        # Different type: ClassDef != FuncDef ,
        # for now just return the updated node
        # Add any missing methods overloads
        updated_node = self.add_missed_overloads(updated_node, stack_id)
        # Add any missing @mp_available
        updated_node = self.add_missed_mp_available(updated_node, stack_id)
        return updated_node

    # ------------------------------------------------------------------------

    # ------------------------------------------------------------------------
    def visit_FunctionDef(self, node: cst.FunctionDef) -> Optional[bool]:
        self.stack.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> Union[cst.FunctionDef, cst.ClassDef]:
        "Update the function Parameters and return type, decorators and docstring"
        if is_getter(updated_node):
            extra = ["getter"]
        elif is_setter(updated_node):
            extra = ["setter"]
        elif is_deleter(updated_node):
            extra = ["deleter"]
        else:
            extra = []
        stack_id = tuple(self.stack + extra)
        self.stack.pop()
        if stack_id not in self.annotations:
            # no changes to the function in docstub
            return updated_node
        if updated_node.decorators and any(is_decorator(dec, "overload") for dec in updated_node.decorators):
            # do not overwrite existing @overload functions
            # ASSUME: they are OK as they are
            return updated_node
        # update the firmware_stub from the doc_stub information
        doc_stub = self.annotations[stack_id].type_info
        if isinstance(doc_stub.def_node, cst.FunctionDef):
            # avoid mismatching property decorators
            # if the updated node is a property, and the doc_stub node is not a property
            # then we should not update the node
            if is_property(updated_node) and not is_property(doc_stub.def_node):
                return updated_node
            if is_setter(updated_node) and not is_setter(doc_stub.def_node):
                return updated_node
            if is_getter(updated_node) and not is_getter(doc_stub.def_node):
                return updated_node
            if is_deleter(updated_node) and not is_deleter(doc_stub.def_node):
                return updated_node

        # Check if it is an @overload decorator
        add_overload = any(is_decorator(dec, "overload") for dec in doc_stub.decorators) and len(self.annotations[stack_id].overloads) >= 1

        # If there are overloads in the documentation, use the first one
        if add_overload:
            log.debug(f"Change to @overload :{updated_node.name.value}")
            # Use the new overload - but with the existing docstring
            doc_stub = self.annotations[stack_id].overloads.pop(0)
            updated_node = self.merge_decorator(original_node, updated_node, stack_id, doc_stub)
            return updated_node

        # If there are overloads in the documentation, use the first one
        add_mpa_deco = any(is_decorator(dec, "mp_available") for dec in doc_stub.decorators) and len(self.annotations[stack_id].mp_available) >= 1
        if add_mpa_deco:
            log.debug(f"Change to @mp_available :{updated_node.name.value}")
            # Use the new @mp_available - but with the existing docstring
            doc_stub = self.annotations[stack_id].mp_available.pop(0)
            updated_node = self.merge_decorator(original_node,updated_node ,stack_id, doc_stub)
            return updated_node

        # assert isinstance(doc_stub, TypeInfo)
        # assert doc_stub
        # first update the docstring
        has_no_docstring = updated_node.get_docstring() is None
        if has_no_docstring or self.copy_docstr:
            # overwrite existing docstring if there was none , or if it is asked
            updated_node = update_def_docstr(updated_node, doc_stub.docstr_node, doc_stub.def_node)

        # Sometimes the MCU stubs and the doc stubs have different types : FunctionDef / ClassDef
        # we need to be careful not to copy over all the annotations if the types are different
        if doc_stub.def_type == "funcdef":
            # Same type, we can copy over the annotations
            # params that should  not be overwritten by the doc-stub ?
            if self.copy_params:
                # we are copying rich type definitions, just assume they are better than what is currently
                # in the destination stub
                overwrite_params = True
            else:
                params_txt = _code(original_node.params)
                overwrite_params = params_txt in [
                    "",
                    "...",
                    "*args, **kwargs",
                    "self",
                    "self, *args, **kwargs",
                    "cls",
                    "cls, *args, **kwargs",
                ]

            # return that should not be overwritten by the doc-stub ?
            overwrite_return = True
            if original_node.returns:
                try:
                    overwrite_return = original_node.returns.annotation.value in [  # type: ignore
                        "Incomplete",
                        "Any",
                        "...",
                    ]
                except AttributeError:
                    pass
            # combine the decorators from the doc-stub and the firmware stub
            new_decorators = []
            if doc_stub.decorators:
                new_decorators.extend(doc_stub.decorators)

            for decorator in updated_node.decorators:
                if _code(decorator) not in [_code(d) for d in new_decorators]:
                    new_decorators.append(decorator)

            # if the method is both a static and a class method, we remove the @classmethod decorator to avoid inconsistencies
            if any(is_decorator(dec, "staticmethod") for dec in new_decorators) and any(
                is_decorator(dec, "classmethod") for dec in new_decorators
            ):
                new_decorators = [dec for dec in new_decorators if dec.decorator.value != "classmethod"]

            return updated_node.with_changes(
                decorators=new_decorators,
                params=doc_stub.params if overwrite_params else updated_node.params,
                returns=doc_stub.returns if overwrite_return else updated_node.returns,
            )

        elif doc_stub.def_type == "classdef":
            # Different type: ClassDef != FuncDef ,
            if doc_stub.def_node and self.replace_functiondef_with_classdef:
                # replace the functionDef with the classdef from the stub file
                return doc_stub.def_node
            # for now just return the updated node
            return updated_node
        else:
            #  just return the updated node
            return updated_node

    def merge_decorator(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef, stack_id, doc_stub):
        if not self.copy_params:
                # we have copied over the entire function definition, no further processing should be done on this node
            doc_stub.def_node = cast(cst.FunctionDef, doc_stub.def_node)
            updated_node = doc_stub.def_node

        else:
                # Save (first) existing docstring if any
            existing_ds = None
            if updated_node.get_docstring():
                    # if there is one , then get it including the layout
                existing_ds = original_node.body.body[0]
                assert isinstance(existing_ds, cst.SimpleStatementLine)

            self.annotations[stack_id].docstring_node = existing_ds
            updated_node = update_def_docstr(doc_stub.def_node, existing_ds)
        return updated_node
