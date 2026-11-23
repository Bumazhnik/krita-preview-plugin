from krita import DockWidgetFactory, DockWidgetFactoryBase, Krita

from .extension import RealtimeOverviewExtension
from .overview_docker import OverviewDocker

Krita.instance().addExtension(RealtimeOverviewExtension(Krita.instance()))
Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("realtime_overview_docker", DockWidgetFactoryBase.DockPosition.DockRight,
                      OverviewDocker))
