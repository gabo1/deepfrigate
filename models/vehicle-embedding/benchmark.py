"""Small reproducible Triton latency sweep for the embedding model."""

import json
import os
import time

import numpy as np
import tritonclient.grpc as grpcclient


def main() -> None:
    client = grpcclient.InferenceServerClient(
        os.getenv("TRITON_URL", "localhost:8011")
    )
    rng = np.random.default_rng(11)
    rows = []
    for batch_size in (1, 2, 4, 8, 16, 32):
        array = rng.normal(
            size=(batch_size, 3, 224, 224)
        ).astype(np.float32)
        infer_input = grpcclient.InferInput(
            "x", array.shape, "FP32"
        )
        infer_input.set_data_from_numpy(array)
        output = grpcclient.InferRequestedOutput("fetch_name_0")
        for _ in range(10):
            client.infer(
                "vehicle-embedding",
                [infer_input],
                outputs=[output],
            )
        samples = []
        for _ in range(50):
            started = time.perf_counter()
            client.infer(
                "vehicle-embedding",
                [infer_input],
                outputs=[output],
            )
            samples.append((time.perf_counter() - started) * 1000)
        rows.append(
            {
                "batch": batch_size,
                "p50_ms": round(float(np.percentile(samples, 50)), 3),
                "p95_ms": round(float(np.percentile(samples, 95)), 3),
                "images_per_second": round(
                    batch_size * 1000 / float(np.mean(samples)), 1
                ),
            }
        )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
