import os
import pandas as pd
import boto3
import sagemaker
import torch
import torch_neuron
from sagemaker.pytorch.model import PyTorchModel
from transformers import TapasTokenizer, TapasForQuestionAnswering
from source.deployer import Deployer
from typing import Any, Dict, List, Tuple


class TAPAS_Deployer(Deployer):
    def get_model_and_tokeniser(self) -> None:
        model_name = "google/tapas-mini-finetuned-wtq"
        self.model = TapasForQuestionAnswering.from_pretrained(model_name)
        self.tokenizer = TapasTokenizer.from_pretrained(model_name)

    def tracing_inputs(self) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        table = pd.DataFrame.from_dict(self.endpoint_testing_query()[0]["data"])
        inputs = self.tokenizer(
            table=table,
            queries=self.endpoint_testing_query()[0]["queries"],
            padding="max_length",
            return_tensors="pt",
        )
        return (
            inputs["input_ids"],
            inputs["attention_mask"],
            inputs["token_type_ids"],
        )

    def trace_model(self) -> None:
        model_neuron = torch_neuron.trace(
            self.model,
            self.tracing_inputs(),
            verbose=1,
            compiler_workdir="./compilation_artifacts",
            strict=False,
        )
        print(model_neuron.graph)
        model_neuron.save("neuron_compiled_model.pt")

    def upload_model_to_s3(self) -> None:
        os.system("tar -czvf model.tar.gz neuron_compiled_model.pt")
        sess = sagemaker.Session()
        bucket = sess.default_bucket()
        model_key = "{}/model/model.tar.gz".format("inf1_compiled_model")
        model_path = f"s3://{bucket}/{model_key}"
        boto3.resource("s3").Bucket(bucket).upload_file("model.tar.gz", model_key)
        print(f"Uploaded model to S3: {model_path}")

    def build_ecr_image(self) -> None:
        os.system("bash ./build_and_push.sh")

    def deploy_ecr_image(self) -> None:
        role = sagemaker.get_execution_role()
        sess = sagemaker.Session()

        bucket = sess.default_bucket()
        prefix = "inf1_compiled_model/model"

        client = boto3.client("sts")
        account = client.get_caller_identity()["Account"]

        my_session = boto3.session.Session()
        region = my_session.region_name

        ecr_image = "{}.dkr.ecr.{}.amazonaws.com/{}:latest".format(
            account, region, self.algorithm_name
        )
        key = os.path.join(prefix, "model.tar.gz")
        pretrained_model_data = "s3://{}/{}".format(bucket, key)

        pytorch_model = PyTorchModel(
            model_data=pretrained_model_data,
            role=role,
            source_dir="entrypoint",
            framework_version="1.7.1",
            entry_point=self.entrypoint_to_use,
            image_uri=ecr_image,
        )

        pytorch_model._is_compiled_model = True
        self.predictor = pytorch_model.deploy(
            initial_instance_count=1, instance_type="ml.inf1.2xlarge"
        )
        self.predictor.serializer = sagemaker.serializers.JSONSerializer()
        self.predictor.deserializer = sagemaker.deserializers.JSONDeserializer()

    def test_endpoint(self) -> Any:
        return self.predictor.predict(self.endpoint_testing_query())

    def terminate(self) -> Dict[str, Any]:
        return self.predictor.delete_endpoint(self.predictor.endpoint)

    def endpoint_testing_query(self) -> List[Dict]:
        data = {
            "Actors": ["Brad Pitt", "Leonardo Di Caprio", "George Clooney"],
            "Number of movies": ["87", "53", "69"],
        }
        queries = [
            "What is the name of the first actor?",
            "How many movies has George Clooney played in?",
            "What is the total number of movies?",
        ]
        return [{"data": data, "queries": queries}]
