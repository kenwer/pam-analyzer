from pam_analyzer.infrastructure.birdnet_lib import region_species_scientific
import sys

lat = 48.503614
lon = 9.048369
week = -1

species = region_species_scientific(lat, lon, week)
print(f"Number of species in region (lat={lat}, lon={lon}, week={week}): {len(species)}")
