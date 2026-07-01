from packages.geo import generate_h3_grid


def main():
    # Example usage of the generate_h3_grid function
    aoi_label = "idf"
    resolution = 10

    generate_h3_grid(aoi_label, resolution)


if __name__ == "__main__":
    main()
