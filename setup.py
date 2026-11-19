from setuptools import setup, find_packages

setup(
    name="cbpi4-BrewVision",
    version="0.3.0",
    description="CraftBeerPi4 listener for Blichmann BrewVision readings",
    author="Brian Thompson",
    license="GPL-3.0",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[],
    entry_points={
        "cbpi4": [
            "BrewVision = cbpi4_BrewVision:setup"
        ]
    }
)
