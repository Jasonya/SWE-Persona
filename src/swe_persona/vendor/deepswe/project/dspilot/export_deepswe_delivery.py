"""DeepSWE-named entrypoint for the offline dataset delivery exporter.

The previous export_tb3_delivery entrypoint remains compatible. TB3 is only
the delivery layout reference; it is not the dataset name or source lineage.
"""
from .export_tb3_delivery import export_delivery, main


if __name__ == '__main__':
    main()
