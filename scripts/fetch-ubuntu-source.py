"""Fetch a superseded exact Ubuntu source revision retained by Launchpad."""
import json
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request


def read_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def main():
    package, version, codename = sys.argv[1:]
    query = urllib.parse.urlencode({
        "ws.op": "getPublishedSources", "source_name": package,
        "version": version, "exact_match": "true",
    })
    publications = read_json("https://api.launchpad.net/1.0/ubuntu/+archive/primary?" + query)
    publication = next((entry for entry in publications["entries"]
                        if entry["source_package_name"] == package
                        and entry["source_package_version"] == version
                        and entry["distro_series_link"].endswith("/" + codename)), None)
    if publication is None:
        raise RuntimeError(f"No retained Ubuntu source revision: {package} {version}")
    files = read_json(publication["self_link"] + "?ws.op=sourceFileUrls")
    descriptors = []
    for url in files:
        name = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        if not name or name in (".", ".."):
            raise RuntimeError("Invalid source artifact filename")
        print(f"Downloading retained Ubuntu source artifact: {name}", flush=True)
        partial = pathlib.Path(name + ".partial")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        partial.replace(name)
        if name.endswith(".dsc"):
            descriptors.append(name)
    if len(descriptors) != 1:
        raise RuntimeError("Expected one source package descriptor")
    # dpkg-source verifies the descriptor's checksums before extraction.
    subprocess.run(["dpkg-source", "-x", descriptors[0]], check=True)


if __name__ == "__main__":
    main()
