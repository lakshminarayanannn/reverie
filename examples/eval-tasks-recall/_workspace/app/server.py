"""Dev server entry point: python -m app.server --port <port>."""
import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    print(f"Serving on http://127.0.0.1:{args.port}")


if __name__ == "__main__":
    main()
