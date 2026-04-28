import QtQuick
import QtQuick.Controls
import QtLocation
import QtPositioning

Item {
    id: root
    width: 600
    height: 400

    signal locationPicked(double latitude, double longitude)
    signal zoomChanged(double zoom)

    property bool _programmatic: false

    MapView {
        id: view
        anchors.fill: parent
        map.plugin: Plugin {
            name: "osm"
            PluginParameter {
                name: "osm.useragent"
                value: "pam-analyzer"
            }
            PluginParameter {
                name: "osm.mapping.custom.host"
                value: "https://tile.openstreetmap.org/"
            }
        }

        Component.onCompleted: {
            for (var i = 0; i < map.supportedMapTypes.length; ++i) {
                if (map.supportedMapTypes[i].name.indexOf("Custom") !== -1) {
                    map.activeMapType = map.supportedMapTypes[i];
                    break;
                }
            }
        }

        map.onZoomLevelChanged: {
            if (!root._programmatic) {
                root.zoomChanged(view.map.zoomLevel)
            }
        }

        map.center: QtPositioning.coordinate(20, 0)
        map.zoomLevel: 2

        TapHandler {
            onTapped: (eventPoint) => {
                var coord = view.map.toCoordinate(eventPoint.position);
                marker.coordinate = coord;
                marker.visible = true;
                root.locationPicked(coord.latitude, coord.longitude);
            }
        }

        MapQuickItem {
            id: marker
            parent: view.map
            coordinate: QtPositioning.coordinate(20, 0)
            anchorPoint: Qt.point(15, 30)
            visible: false

            sourceItem: Rectangle {
                id: markerRect
                width: 30
                height: 30
                radius: 15
                color: "red"
                border.color: "white"
                border.width: 3

                Rectangle {
                    anchors.centerIn: parent
                    width: 12
                    height: 12
                    radius: 6
                    color: "white"
                }

                DragHandler {
                    onActiveChanged: {
                        if (!active) {
                            var scenePos = centroid.scenePosition;
                            var mapPos = view.map.mapFromItem(null, scenePos.x, scenePos.y);
                            var newCoord = view.map.toCoordinate(Qt.point(mapPos.x, mapPos.y));
                            markerRect.x = 0;
                            markerRect.y = 0;
                            marker.coordinate = newCoord;
                            root.locationPicked(newCoord.latitude, newCoord.longitude);
                        }
                    }
                }
            }
        }
    }

    function setMarker(lat, lon, zoom) {
        root._programmatic = true;
        view.map.center = QtPositioning.coordinate(lat, lon);
        view.map.zoomLevel = zoom;
        marker.coordinate = QtPositioning.coordinate(lat, lon);
        marker.visible = true;
        root._programmatic = false;
    }

    function clearMarker() {
        root._programmatic = true;
        marker.visible = false;
        view.map.center = QtPositioning.coordinate(20, 0);
        view.map.zoomLevel = 2;
        root._programmatic = false;
    }
}
