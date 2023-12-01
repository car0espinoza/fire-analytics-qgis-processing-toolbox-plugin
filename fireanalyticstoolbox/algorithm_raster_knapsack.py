# -*- coding: utf-8 -*-
"""
/***************************************************************************
 ProcessingPluginClass
                                 A QGIS plugin
 Description of the p p
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2023-07-12
        copyright            : (C) 2023 by fdo
        email                : fbadilla@ing.uchile.cl
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 pyomo.common.errors.ApplicationError: No Python bindings available for solver plugin
Traceback (most recent call last):
  File "/home/fdo/.local/share/QGIS/QGIS3/profiles/default/python/plugins/fireanalyticstoolbox/algorithm_raster_knapsack.py", line 164, in initAlgorithm
    if SolverFactory(solver).available():
       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/fdo/pyenv/pyv/lib/python3.11/site-packages/pyomo/solvers/plugins/solvers/direct_or_persistent_solver.py", line 313, in available
    raise ApplicationError(
pyomo.common.errors.ApplicationError: No Python bindings available for  solver plugin
"""
__author__ = "fdo"
__date__ = "2023-07-12"
__copyright__ = "(C) 2023 by fdo"
__version__ = "$Format:%H$"

from contextlib import redirect_stderr, redirect_stdout
# from functools import reduce
from io import StringIO
from itertools import compress
from os import environ, pathsep, sep
from pathlib import Path
from platform import system as platform_system
from shutil import which
from time import sleep

import numpy as np
from grassprovider.Grass7Utils import Grass7Utils
from osgeo import gdal
from pandas import DataFrame
from processing.tools.system import getTempFilename
from pyomo import environ as pyo
from pyomo.common.errors import ApplicationError
from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from qgis.core import (Qgis, QgsFeatureSink, QgsMessageLog, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingFeedback, QgsProcessingParameterDefinition,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString, QgsProject, QgsRasterBlock, QgsRasterFileWriter)
from qgis.PyQt.QtCore import QByteArray, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from scipy import stats

from .algorithm_utils import run_alg_styler_bin, write_log
from .config import METRICS, NAME, SIM_OUTPUTS, STATS, TAG, jolo

NODATA = -1  # -32768
SOLVER = {
    "cbc": "ratioGap=0.005 seconds=300",
    "glpk": "mipgap=0.005 tmlim=300",
    "ipopt": "",
    "gurobi": "MIPGap=0.005 TimeLimit=300",
    "cplex_direct": "mipgap=0.005 timelimit=300",
}


def get_pyomo_available_solvers():
    pyomo_solvers_list = pyo.SolverFactory.__dict__["_cls"].keys()
    solvers_filter = []
    for s in pyomo_solvers_list:
        try:
            solvers_filter.append(pyo.SolverFactory(s).available())
        except (ApplicationError, NameError, ImportError) as e:
            solvers_filter.append(False)
    pyomo_solvers_list = list(compress(pyomo_solvers_list, solvers_filter))
    return pyomo_solvers_list


def add_cbc_to_path():
    """Add cbc to path if it is not already there"""
    if which("cbc.exe") is None and "__file__" in globals():
        cbc_exe = Path(__file__).parent / "cbc" / "bin" / "cbc.exe"
        if cbc_exe.is_file():
            environ["PATH"] += pathsep + str(cbc_exe.parent)
            QgsMessageLog.logMessage(f"Added {cbc_exe} to path")


class RasterKnapsackAlgorithm(QgsProcessingAlgorithm):
    """
    This is an example algorithm that takes a vector layer and
    creates a new identical one.

    It is meant to be used as an example of how to create your own
    algorithms and explain methods and variables used to do it. An
    algorithm like this will be available in all elements, and there
    is not need for additional work.

    All Processing algorithms should extend the QgsProcessingAlgorithm
    class.
    """

    # Constants used to refer to parameters and outputs. They will be
    # used when calling the algorithm from another algorithm, or when
    # calling from the QGIS console.
    OUTPUT_layer = "OUTPUT_layer"
    OUTPUT_csv = "OUTPUT_csv"
    INPUT_value = "INPUT_value"
    INPUT_weight = "INPUT_weight"
    INPUT_ratio = "INPUT_ratio"
    INPUT_executable = "INPUT_executable_path"

    solver_exception_msg = ""

    if platform_system() == "Windows":
        add_cbc_to_path()

    def initAlgorithm(self, config):
        """
        Here we define the inputs and output of the algorithm, along
        with some other properties.
        """
        # value
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                name=self.INPUT_value,
                description=self.tr("Values layer (if blank 1's will be used)"),
                defaultValue=[QgsProcessing.TypeRaster],
                optional=True,
            )
        )
        # weight
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                name=self.INPUT_weight,
                description=self.tr("Weights layer (if blank 1's will be used)"),
                defaultValue=[QgsProcessing.TypeRaster],
                optional=True,
            )
        )
        # ratio double
        qppn = QgsProcessingParameterNumber(
            name=self.INPUT_ratio,
            description=self.tr("Capacity ratio (1 = weight.sum)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.069,
            optional=False,
            minValue=0.0,
            maxValue=1.0,
        )
        qppn.setMetadata({"widget_wrapper": {"decimals": 3}})
        self.addParameter(qppn)
        # raster output
        # RasterDestinationGpkg inherits from QgsProcessingParameterRasterDestination to set output format
        self.addParameter(RasterDestinationGpkg(self.OUTPUT_layer, self.tr("Output layer")))
        # SOLVERS
        # check availability
        solver_available = [False] * len(SOLVER)
        for i, solver in enumerate(SOLVER):
            try:
                if SolverFactory(solver).available():
                    solver_available[i] = True
            except Exception as e:
                self.solver_exception_msg += f"solver:{solver}, problem:{e}\n"
        # prepare hints
        value_hints = []
        for i, (k, v) in enumerate(SOLVER.items()):
            if solver_available[i]:
                value_hints += [f"{k}: {v}"]
            else:
                value_hints += [f"{k}: {v} MUST SET EXECUTABLE"]
        # solver string combobox (enums
        qpps = QgsProcessingParameterString(
            name="SOLVER",
            description="Solver: recommended options string [and executable STATUS]",
        )
        qpps.setMetadata({
            "widget_wrapper": {
                "value_hints": value_hints,
                "setEditable": True,  # not working
            }
        })
        qpps.setFlags(qpps.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps)
        # options_string
        qpps2 = QgsProcessingParameterString(
            name="CUSTOM_OPTIONS_STRING",
            description="Override options_string (type a single space ' ' to not send any options to the solver)",
            defaultValue="",
            optional=True,
        )
        qpps2.setFlags(qpps2.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qpps2)
        # executable file
        qppf = QgsProcessingParameterFile(
            name=self.INPUT_executable,
            description=self.tr("Set solver executable file [REQUIRED if STATUS]"),
            behavior=QgsProcessingParameterFile.File,
            extension="exe" if platform_system() == "Windows" else "",
            optional=True,
        )
        qppf.setFlags(qppf.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(qppf)

    def processAlgorithm(self, parameters, context, feedback):
        # feedback.pushCommandInfo(f"processAlgorithm START")
        # feedback.pushCommandInfo(f"parameters {parameters}")
        # feedback.pushCommandInfo(f"context args: {context.asQgisProcessArguments()}")

        # ?
        # feedback.reportError(f"context.logLevel(): {context.logLevel()}")
        # context.setLogLevel(context.logLevel()+1)

        # report solver unavailability
        feedback.pushInfo(f"Solver unavailability:\n{self.solver_exception_msg}\n")

        # get raster data
        value_layer = self.parameterAsRasterLayer(parameters, self.INPUT_value, context)
        value_data = get_raster_data(value_layer)
        value_nodata = get_raster_nodata(value_layer, feedback)
        value_map_info = get_raster_info(value_layer)

        weight_layer = self.parameterAsRasterLayer(parameters, self.INPUT_weight, context)
        weight_data = get_raster_data(weight_layer)
        weight_nodata = get_raster_nodata(weight_layer, feedback)
        weight_map_info = get_raster_info(weight_layer)

        # raster(s) conditions
        if not value_layer and not weight_layer:
            feedback.reportError("No input layers, need at least one raster!")
            return {self.OUTPUT_layer: None, "SOLVER_STATUS": None, "SOLVER_TERMINATION_CONDITION": None}
        elif value_layer and weight_layer:
            if not (
                value_map_info["width"] == weight_map_info["width"]
                and value_map_info["height"] == weight_map_info["height"]
                and value_map_info["cellsize_x"] == weight_map_info["cellsize_x"]
                and value_map_info["cellsize_y"] == weight_map_info["cellsize_y"]
            ):
                feedback.reportError("Layers must have the same width, height and cellsizes")
                return {self.OUTPUT_layer: None, "SOLVER_STATUS": None, "SOLVER_TERMINATION_CONDITION": None}
            width, height, extent, crs, _, _ = value_map_info.values()
        elif value_layer and not weight_layer:
            width, height, extent, crs, _, _ = value_map_info.values()
            weight_data = np.ones(height * width)
        elif not value_layer and weight_layer:
            width, height, extent, crs, _, _ = weight_map_info.values()
            value_data = np.ones(height * width)

        # instance summary
        N = width * height

        feedback.pushInfo(
            f"width: {width}, height: {height}, N:{N}\n"
            f"extent: {extent}, crs: {crs}\n"
            "\n"
            f"value !=0: {np.any(value_data!=0)}\n"
            f" nodata: {value_nodata}\n"
            f" preview: {value_data}\n"
            f" stats: {stats.describe(value_data[value_data!=value_nodata])}\n"
            "\n"
            f"weight !=1: {np.any(weight_data!=1)}\n"
            f" nodata: {weight_nodata}\n"
            f" preview: {weight_data}\n"
            f" stats: {stats.describe(weight_data[weight_data!=weight_nodata])}\n"
        )

        if isinstance(value_nodata, list):
            feedback.pushError(f"value_nodata: {value_nodata} is list, not implemented!")
        if isinstance(weight_nodata, list):
            feedback.pushError(f"weight_nodata: {weight_nodata} is list, not implemented!")

        no_indexes = np.union1d(np.where(value_data == value_nodata)[0], np.where(weight_data == weight_nodata)[0])
        # no_indexes = reduce(
        #     np.union1d,
        #     (
        #         np.where(value_data == value_nodata)[0],
        #         np.where(value_data == 0)[0],
        #         np.where(weight_data == weight_nodata)[0],
        #     ),
        # )
        feedback.pushInfo(f"discarded pixels (no_indexes): {len(no_indexes)/N:.2%}\n")
        mask = np.ones(N, dtype=bool)
        mask[no_indexes] = False

        ratio = self.parameterAsDouble(parameters, self.INPUT_ratio, context)
        weight_sum = weight_data[mask].sum()
        capacity = round(weight_sum * ratio)
        feedback.pushInfo(f"capacity bound: ratio {ratio}, weight_sum: {weight_sum}, capacity: {capacity}\n")

        feedback.setProgress(10)
        feedback.setProgressText(f"rasters processed 10%")

        m = pyo.ConcreteModel()
        m.N = pyo.RangeSet(0, N - len(no_indexes) - 1)
        m.Cap = pyo.Param(initialize=capacity)
        m.We = pyo.Param(m.N, within=pyo.Reals, initialize=weight_data[mask])
        m.Va = pyo.Param(m.N, within=pyo.Reals, initialize=value_data[mask])
        m.X = pyo.Var(m.N, within=pyo.Binary)
        obj_expr = pyo.sum_product(m.X, m.Va, index=m.N)
        m.obj = pyo.Objective(expr=obj_expr, sense=pyo.maximize)

        def capacity_rule(m):
            return pyo.sum_product(m.X, m.We, index=m.N) <= m.Cap

        m.capacity = pyo.Constraint(rule=capacity_rule)

        executable = self.parameterAsString(parameters, self.INPUT_executable, context)
        # feedback.pushDebugInfo(f"exesolver_string:{executable}")

        solver_string = self.parameterAsString(parameters, "SOLVER", context)
        # feedback.pushDebugInfo(f"solver_string:{solver_string}")

        solver_string = solver_string.replace(" MUST SET EXECUTABLE", "")

        solver, options_string = solver_string.split(": ", 1) if ": " in solver_string else (solver_string, "")
        # feedback.pushDebugInfo(f"solver:{solver}, options_string:{options_string}")

        if len(custom_options := self.parameterAsString(parameters, "CUSTOM_OPTIONS_STRING", context)) > 0:
            if custom_options == " ":
                options_string = None
            else:
                options_string = custom_options
        feedback.pushDebugInfo(f"options_string: {options_string}\n")

        if executable:
            opt = SolverFactory(solver, executable=executable)
        else:
            opt = SolverFactory(solver)

        feedback.setProgress(20)
        feedback.setProgressText("pyomo model built, solver object created 20%")

        pyomo_std_feedback = FileLikeFeedback(feedback, True)
        pyomo_err_feedback = FileLikeFeedback(feedback, False)
        with redirect_stdout(pyomo_std_feedback), redirect_stderr(pyomo_err_feedback):
            if options_string:
                results = opt.solve(m, tee=True, options_string=options_string)
            else:
                results = opt.solve(m, tee=True)
            # TODO
            # # Stop the algorithm if cancel button has been clicked
            # if feedback.isCanceled():

        status = results.solver.status
        termCondition = results.solver.termination_condition
        feedback.pushConsoleInfo(f"Solver status: {status}, termination condition: {termCondition}")

        if (
            status in [SolverStatus.error, SolverStatus.aborted, SolverStatus.unknown]
            and termCondition != TerminationCondition.intermediateNonInteger
        ):
            feedback.reportError(f"Solver status: {status}, termination condition: {termCondition}")
            return {self.OUTPUT_layer: None, "SOLVER_STATUS": status, "SOLVER_TERMINATION_CONDITION": termCondition}
        if termCondition in [
            TerminationCondition.infeasibleOrUnbounded,
            TerminationCondition.infeasible,
            TerminationCondition.unbounded,
        ]:
            feedback.reportError(f"Optimization problem is {termCondition}. No output is generated.")
            return {self.OUTPUT_layer: None, "SOLVER_STATUS": status, "SOLVER_TERMINATION_CONDITION": termCondition}
        if not termCondition == TerminationCondition.optimal:
            feedback.pushWarning(
                "Output is generated for a non-optimal solution! Try running again with different solver options or"
                " tweak the layers..."
            )

        feedback.setProgress(90)
        feedback.setProgressText("pyomo integer programming finished, progress 80%")

        # pyomo solution to squared numpy array
        response = np.array([pyo.value(m.X[i], exception=False) for i in m.X])
        response[response == None] = NODATA
        response = response.astype(np.int16)
        base = -np.ones(N, dtype=np.int16)
        base[mask] = response
        base.resize(height, width)

        output_layer_filename = self.parameterAsOutputLayer(parameters, self.OUTPUT_layer, context)
        outFormat = Grass7Utils.getRasterFormatFromFilename(output_layer_filename)

        nodatas, zeros, ones = np.histogram(base, bins=[NODATA, 0, 1, 2])[0]
        feedback.pushInfo(
            "Generated layer histogram:\n"
            f" No data or not selected: {zeros}\n"
            f" Selected               : {ones}\n"
            f" Solver returned None   : {nodatas}\n"
            f"Output format: {outFormat}"
        )
        array2rasterInt16(
            base,
            "knapsack",
            output_layer_filename,
            extent,
            crs,
            nodata=NODATA,
        )
        feedback.setProgress(100)
        feedback.setProgressText("Writing new raster to file ended, progress 100%")

        # if showing
        if context.willLoadLayerOnCompletion(output_layer_filename):
            layer_details = context.layerToLoadOnCompletionDetails(output_layer_filename)
            layer_details.setPostProcessor(
                run_alg_styler_bin("Knapsack Raster", color0=(105, 236, 172), color1=(238, 80, 154))
            )
            layer_details.groupName = "Recommendations"
            layer_details.layerSortKey = 2

        write_log(feedback, name=self.name())
        return {
            self.OUTPUT_layer: output_layer_filename,
            "SOLVER_STATUS": status,
            "SOLVER_TERMINATION_CONDITION": termCondition,
        }

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm. This
        string should be fixed for the algorithm, and must not be localised.
        The name should be unique within each provider. Names should contain
        lowercase alphanumeric characters only and no spaces or other
        formatting characters.
        """
        return "rasterknapsackoptimization"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Raster Knapsack Optimization")

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return RasterKnapsackAlgorithm()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """Optimizes the classical knapsack problem using layers as values and/or weights, returns a layer with the selected pixels."""
        )

    def shortHelpString(self):
        return self.tr(
            """By selecting a Values layer and/or a Weights layer, and setting the bound on the total capacity, a layer that maximizes the sum of the values of the selected pixels is created.

A new .gpkg raster will show selected pixels in red and non-selected green (values 1, 0 and no-data=-1).

The capacity constraint is set up by choosing a ratio (between 0 and 1), that multiplies the sum of all weights (except no-data). Hence 1 selects all pixels that aren't no-data in both layers.

This raster knapsack problem is NP-hard, so a MIP solver engine is used to find "nearly" the optimal solution (**), because -often- is asymptotically hard to prove the optimal value. So a default gap of 0.5% and a timelimit of 5 minutes cuts off the solver run. The user can experiment with these parameters to trade-off between accuracy, speed and instance size(*). On Windows closing the blank terminal window will abort the run!

By using Pyomo, several MIP solvers can be used: CBC, GLPK, Gurobi, CPLEX or Ipopt; If they're accessible through the system PATH, else the executable file can be selected by the user. Installation of solvers is up to the user, although the windows version is bundled with CBC unsigned binaries, so their users will face a "Windows protected your PC" warning, please avoid pressing the "Don't run" button, follow the "More info" link, scroll then press "Run anyway".

(*): Complexity can be reduced greatly by rescaling and/or rounding values into integers, or even better coarsing the raster resolution (see gdal translate resolution).
(**): There are specialized knapsack algorithms that solve in polynomial time, but not for every data type combination; hence using a MIP solver is the most flexible approach.

----

USE CASE:

If you want to determine where to allocate fuel treatments throughout the landscape to protect a specific value that is affected by both the fire and the fuel treatments, use the following:

    - Values: Downstream Protection Value layer calculated with the respective value that you want to protect.

    - Weights: The layer, that contains the value that you want to protect and that is affected also by the fuel treatments (e.g., animal habitat).
If you want to determine where to allocate fuel treatments through out the landscape to protect and specific value that is affected by both, the fire and the fuel treatments use: 
"""
        )

    def helpString(self):
        return self.shortHelpString()

    def icon(self):
        return QIcon(":/plugins/fireanalyticstoolbox/assets/firebreakmap.svg")


class RasterDestinationGpkg(QgsProcessingParameterRasterDestination):
    """overrides the defaultFileExtension method to gpkg
    ALTERNATIVE:
    from types import MethodType
    QPPRD = QgsProcessingParameterRasterDestination(self.OUTPUT_layer, self.tr("Output layer"))
    def _defaultFileExtension(self):
        return "gpkg"
    QPPRD.defaultFileExtension = MethodType(_defaultFileExtension, QPPRD)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def defaultFileExtension(self):
        return "gpkg"


def get_raster_data(layer):
    """raster layer into numpy array
        slower alternative:
            for i in range(lyr.width()):
                for j in range(lyr.height()):
                    values.append(block.value(i,j))
    # npArr = np.frombuffer( qByteArray)  #,dtype=float)
    # return npArr.reshape( (layer.height(),layer.width()))
    """
    if layer:
        provider = layer.dataProvider()
        if numpy_dtype := qgis2numpy_dtype(provider.dataType(1)):
            block = provider.block(1, layer.extent(), layer.width(), layer.height())
            qByteArray = block.data()
            return np.frombuffer(qByteArray, dtype=numpy_dtype)


def get_raster_nodata(layer, feedback):
    if layer:
        dp = layer.dataProvider()
        if dp.sourceHasNoDataValue(1):
            ndv = dp.sourceNoDataValue(1)
            feedback.pushInfo(f" nodata: {ndv}")
            return ndv


def get_raster_info(layer):
    if layer:
        return {
            "width": layer.width(),
            "height": layer.height(),
            "extent": layer.extent(),
            "crs": layer.crs(),
            "cellsize_x": layer.rasterUnitsPerPixelX(),
            "cellsize_y": layer.rasterUnitsPerPixelY(),
        }


class FileLikeFeedback(StringIO):
    def __init__(self, feedback, std):
        super().__init__()
        if std:
            self.print = feedback.pushConsoleInfo
        else:
            self.print = feedback.pushWarning
        # self.std = std
        # self.feedback = feedback
        # self.feedback.pushDebugInfo(f"{self.std} FileLikeFeedback init")

    def write(self, msg):
        # self.feedback.pushDebugInfo(f"{self.std} FileLikeFeedback write")
        super().write(msg)
        self.flush()

    def flush(self):
        self.print(super().getvalue())
        super().__init__()
        # self.feedback.pushDebugInfo(f"{self.std} FileLikeFeedback flush")


# class FileLikeFeedback:
#     def __init__(self, feedback):
#         super().__init__()
#         self.feedback = feedback
#     def write(self, msg):
#        self.msg+=msg
#     def flush(self):
#        self.feedback.pushConsoleInfo(self.msg)
#        self.msg = ""


def array2rasterInt16(data, name, geopackage, extent, crs, nodata=None):
    """numpy array to gpkg casts to name"""
    data = np.int16(data)
    h, w = data.shape
    bites = QByteArray(data.tobytes())
    block = QgsRasterBlock(Qgis.CInt16, w, h)
    block.setData(bites)
    fw = QgsRasterFileWriter(str(geopackage))
    fw.setOutputFormat("gpkg")
    fw.setCreateOptions(["RASTER_TABLE=" + name, "APPEND_SUBDATASET=YES"])
    provider = fw.createOneBandRaster(Qgis.Int16, w, h, extent, crs)
    provider.setEditable(True)
    if nodata != None:
        provider.setNoDataValue(1, nodata)
    provider.writeBlock(block, 1, 0, 0)
    provider.setEditable(False)


def adjust_value_scale(a):
    """Check if all values are positive or negative"""
    if len(a) not in [len(a[a >= 0]), len(a[a <= 0])]:
        return a + a.min() + 1
    return a


def qgis2numpy_dtype(qgis_dtype: Qgis.DataType) -> np.dtype:
    """Conver QGIS data type to corresponding numpy data type
    https://raw.githubusercontent.com/PUTvision/qgis-plugin-deepness/fbc99f02f7f065b2f6157da485bef589f611ea60/src/deepness/processing/processing_utils.py
    This is modified and extended copy of GDALDataType.

    * ``UnknownDataType``: Unknown or unspecified type
    * ``Byte``: Eight bit unsigned integer (quint8)
    * ``Int8``: Eight bit signed integer (qint8) (added in QGIS 3.30)
    * ``UInt16``: Sixteen bit unsigned integer (quint16)
    * ``Int16``: Sixteen bit signed integer (qint16)
    * ``UInt32``: Thirty two bit unsigned integer (quint32)
    * ``Int32``: Thirty two bit signed integer (qint32)
    * ``Float32``: Thirty two bit floating point (float)
    * ``Float64``: Sixty four bit floating point (double)
    * ``CInt16``: Complex Int16
    * ``CInt32``: Complex Int32
    * ``CFloat32``: Complex Float32
    * ``CFloat64``: Complex Float64
    * ``ARGB32``: Color, alpha, red, green, blue, 4 bytes the same as QImage.Format_ARGB32
    * ``ARGB32_Premultiplied``: Color, alpha, red, green, blue, 4 bytes  the same as QImage.Format_ARGB32_Premultiplied
    """
    if qgis_dtype == Qgis.DataType.Byte:
        return np.uint8
    if qgis_dtype == Qgis.DataType.UInt16:
        return np.uint16
    if qgis_dtype == Qgis.DataType.Int16:
        return np.int16
    if qgis_dtype == Qgis.DataType.Float32:
        return np.float32
    if qgis_dtype == Qgis.DataType.Float64:
        return np.float64
