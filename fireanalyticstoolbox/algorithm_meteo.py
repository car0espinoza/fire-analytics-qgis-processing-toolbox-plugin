# -*- coding: utf-8 -*-
"""
/***************************************************************************
 FireToolbox
                                 A QGIS plugin
 A collection of fire insights related algorithms
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2024-04-21
        copyright            : (C) 2024 by Fernando Badilla Veliz - Fire2a.com
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
"""
__author__ = "Fernando Badilla Veliz - Fire2a.com"
__date__ = "2023-04-21"
__copyright__ = "(C) 2024 by Fernando Badilla Veliz - Fire2a.com"

# This will get replaced with a git SHA1 when you do a git archive

__revision__ = "$Format:%H$"


from datetime import datetime
from pathlib import Path

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingParameterDateTime, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterVectorLayer, QgsProject)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.utils import iface

from .algorithm_utils import write_log


class MeteoAlgo(QgsProcessingAlgorithm):

    IN_LOCATION = "location"
    IN_DATE = "start_date"
    IN_ROWRES = "time_resolution"
    IN_NUMROWS = "time_lenght"
    IN_NUMSIMS = "number_of_scenarios"
    OUT = "output_directory"
    now = datetime.now()

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                name=self.IN_LOCATION,
                description="Where? Single point vector layer, else the center of the current map will be used.",
                types=[QgsProcessing.TypeVectorPoint],
                defaultValue=None,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterDateTime(
                self.IN_DATE,
                self.tr("Start timestamp"),
                defaultValue=self.now,
                optional=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IN_ROWRES,
                self.tr("Step resolution in minutes (time between rows)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=60,
                minValue=1,
                optional=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IN_NUMROWS,
                self.tr("Lenght of each scenario (number of rows)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=12,
                minValue=1,
                optional=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IN_NUMSIMS,
                self.tr("Number of scenarios to generate"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=1,
                maxValue=100000,
                optional=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUT,
                self.tr("Output folder"),
                # defaultValue=default_fd,
                defaultValue=QgsProject().instance().absolutePath(),
            )
        )

    def checkParameterValues(self, parameters, context):
        outdir = Path(self.parameterAsFile(parameters, self.OUT, context))
        if outdir.is_dir():
            # check if empty
            if list(outdir.iterdir()) != []:
                return False, f"{outdir} is not empty"
        return True, ""

    def processAlgorithm(self, parameters, context, feedback):
        """
        date = datetime.now()
        rowres = 60
        numrows = 12
        numsims = 1
        outdir = Path('.')
        """
        instance = {
            "start_datetime": self.parameterAsDateTime(parameters, self.IN_DATE, context).toPyDateTime(),
            "rowres": self.parameterAsInt(parameters, self.IN_ROWRES, context),
            "numrows": self.parameterAsInt(parameters, self.IN_NUMROWS, context),
            "numsims": self.parameterAsInt(parameters, self.IN_NUMSIMS, context),
            "outdir": Path(self.parameterAsFile(parameters, self.OUT, context)),
        }
        if point_lyr := self.parameterAsVectorLayer(parameters, self.IN_LOCATION, context):
            crs = point_lyr.crs()
            for feature in point_lyr.getFeatures():
                point = feature.geometry().asPoint()
                break
            feedback.pushDebugInfo(
                f"Reading from {point_lyr.name()=}, {point_lyr.crs()=}, {feature.id()=}, {feature.geometry().asWkt()=}"
            )
        else:
            point = iface.mapCanvas().center()
            crs = iface.mapCanvas().mapSettings().destinationCrs()
            feedback.pushInfo(f"No location provided, using center of current map {point=}, {crs=}")
        # transform point to epsg:4326
        source_crs = QgsCoordinateReferenceSystem(crs)  # Replace with your CRS ID
        destination_crs = QgsCoordinateReferenceSystem(4326)  # EPSG:4326 for WGS 84
        crs_transform = QgsCoordinateTransform(source_crs, destination_crs, QgsProject.instance())
        transformed_point = crs_transform.transform(point)
        x, y = transformed_point.x(), transformed_point.y()
        feedback.pushDebugInfo(f"{x=}, {y=}")
        instance["x"] = x
        instance["y"] = y

        feedback.pushInfo(f"Generating {instance=}")

        # Call the weather generator
        # ==========================
        # FIXME REMOVE IN PRODUCTION 0
        from importlib import reload
        from fire2a import meteo
        reload(meteo)
        from fire2a.meteo import generate as generater_weather
        # FIXME REMOVE IN PRODUCTION 1
        retval, output_dict = generater_weather(**instance)

        if retval == 0:
            feedback.pushInfo(f"Generated {len(output_dict['filelist'])=} {output_dict['filelist'][:10]=} etc.")
            feedback.pushDebugInfo(f"{output_dict=}") # FIXME REMOVE IN PRODUCTION
            write_log(feedback, name=self.name())
            return {self.OUT: str(instance["outdir"]), "filelist": output_dict["filelist"]}
        elif retval >= 0:
            write_log(feedback, name=self.name())
            raise QgsProcessingException(f"Error {retval=} generating weather scenarios {output_dict=}")
            return {self.OUT: str(instance["outdir"]), "filelist": output_dict["filelist"]}

    def name(self):
        """processing.run('provider:name',{..."""
        return "meteo"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("Meteo")

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return "zexperimental"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return MeteoAlgo()

    def helpUrl(self):
        return "https://www.github.com/fdobad/qgis-processingplugin-template/issues"

    def shortDescription(self):
        return self.tr(
            """This algorithm generates weather scenarios for the Cell2Fire fire simulator and the Kitral fuel model. 
            These scenarios use real data from weather stations and are valid for Chile from the Valparaíso region to the Araucanía region.<br>
            
            <b>Args:</b><br>
            
            - <b>location</b>: Is used to select the weather stations closest to this point.
            The first point of a vector layer will be used, else the center of the map is calculated. <b>Only Chile between 32S and 40S makes sense</b><br>
            - <b>start timestamp</b>: NA Reference historical data of the last 5 years<br>
            - <b>step resolution</b>: NA number of rows in the output raster (it's meaning can later be changed using the --Weather-Period-Length cli argument that defaults to 60<br>
            -<b> Length of each scenario </b>: Indicates the duration, in hours, of each scenario.  <br>
            - <b>number_of_simulations</b>: files to generate<br>
            <b>Returns:</b><br>
            - <b>output_directory</b>: folder where the files are saved containing:<br>
            - Weather(*).csv numbered files with each weather scenario<br>
            - TBI: vector layer representing the weather scenarios as arrows<br>
            """
        )
