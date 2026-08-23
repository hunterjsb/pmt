//! Golden-vector test for deposit-wallet (PM_SIGNATURE_TYPE=3 / POLY_1271)
//! order signing.
//!
//! Drives the SDK's REAL `Client::sign` — the same call `place_limit_order_timed`
//! makes — and asserts the bytes it produces equal what py-clob-client-v2 1.1.0
//! produced for the same order. Type 3 is the one signature shape the CLOB
//! can reject for reasons a unit test on our own code could never see: the
//! order is signed through an ERC-7739 wrapper the deposit wallet contract
//! re-derives, so any drift in a hash, a constant, or a byte offset is silent
//! until a live order bounces.
//!
//! Vectors are frozen in `tests/vectors/poly1271.json`; regenerate ONLY from
//! the Python reference (`tests/vectors/gen_poly1271_vectors.py`), never from
//! this side. Runs fully offline — `sign()`'s single network call (the token's
//! neg-risk flag, which picks the exchange contract) is answered by a loopback
//! stub.
//!
//! Every key here is a throwaway test key.

use std::str::FromStr;

use alloy::primitives::{Address, Signature, B256, U256};
use alloy::signers::local::LocalSigner;
use alloy::signers::Signer;
use polymarket_client_sdk_v2::auth::{Credentials, Uuid};
use polymarket_client_sdk_v2::clob::client::{Client, Config as SdkConfig};
use polymarket_client_sdk_v2::clob::types::{
    OrderPayload, OrderType, OrderV2, SignableOrder, SignatureType,
};
use polymarket_client_sdk_v2::POLYGON;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const VECTORS: &str = include_str!("vectors/poly1271.json");

/// Owner EOA key from the vector generator. Never funded.
const EOA_KEY: &str = "0x0000000000000000000000000000000000000000000000000000000000000001";

/// Answers the one GET `sign()` makes (`/neg-risk`) with a fixed flag, then
/// closes. Keeps the test offline without pulling in a mock-server crate.
async fn neg_risk_stub(neg_risk: bool) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
    let addr = listener.local_addr().expect("stub addr");
    tokio::spawn(async move {
        while let Ok((mut sock, _)) = listener.accept().await {
            let mut buf = [0_u8; 2048];
            let _ = sock.read(&mut buf).await;
            let body = format!(r#"{{"neg_risk":{}}}"#, neg_risk);
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = sock.write_all(resp.as_bytes()).await;
            let _ = sock.flush().await;
        }
    });
    format!("http://{}/", addr)
}

fn vectors() -> serde_json::Value {
    serde_json::from_str(VECTORS).expect("frozen vectors parse")
}

fn case(name: &str) -> serde_json::Value {
    vectors()["cases"]
        .as_array()
        .expect("cases array")
        .iter()
        .find(|c| c["name"] == name)
        .unwrap_or_else(|| panic!("no vector case '{name}'"))
        .clone()
}

fn u256(v: &serde_json::Value) -> U256 {
    U256::from_str(v.as_str().expect("numeric string")).expect("u256")
}

fn addr(v: &serde_json::Value) -> Address {
    Address::from_str(v.as_str().expect("address string")).expect("address")
}

fn b256(v: &serde_json::Value) -> B256 {
    B256::from_str(v.as_str().expect("bytes32 string")).expect("bytes32")
}

/// Rebuild the exact order struct the vector was signed over. Fields are set
/// individually rather than via a literal because the SDK marks `OrderV2`
/// `#[non_exhaustive]`.
fn signable_from(vector: &serde_json::Value) -> SignableOrder {
    let o = &vector["order"];
    let mut order = OrderV2::default();
    order.salt = u256(&o["salt"]);
    order.maker = addr(&o["maker"]);
    order.signer = addr(&o["signer"]);
    order.tokenId = u256(&o["tokenId"]);
    order.makerAmount = u256(&o["makerAmount"]);
    order.takerAmount = u256(&o["takerAmount"]);
    order.side = o["side"].as_u64().expect("side") as u8;
    order.signatureType = o["signatureType"].as_u64().expect("signatureType") as u8;
    order.timestamp = u256(&o["timestamp"]);
    order.metadata = b256(&o["metadata"]);
    order.builder = b256(&o["builder"]);

    let mut signable = SignableOrder::default();
    signable.payload = OrderPayload::new(order, u256(&o["expiration"]));
    signable.order_type = OrderType::GTC;
    signable
}

/// Authenticate without touching the network: supplying credentials skips the
/// L1 derive call, and the host points at the neg-risk stub.
async fn signing_client(
    host: &str,
    signer: &LocalSigner<alloy::signers::k256::ecdsa::SigningKey>,
    sig_type: SignatureType,
    funder: Option<Address>,
) -> Client<polymarket_client_sdk_v2::auth::state::Authenticated<polymarket_client_sdk_v2::auth::Normal>>
{
    let mut builder = Client::new(host, SdkConfig::default())
        .expect("client")
        .authentication_builder(signer)
        .signature_type(sig_type)
        .credentials(Credentials::new(
            Uuid::nil(),
            "dGVzdC1zZWNyZXQ=".to_string(),
            "test-passphrase".to_string(),
        ));
    if let Some(f) = funder {
        builder = builder.funder(f);
    }
    builder.authenticate().await.expect("authenticate offline")
}

fn test_signer() -> LocalSigner<alloy::signers::k256::ecdsa::SigningKey> {
    LocalSigner::from_str(EOA_KEY)
        .expect("test key")
        .with_chain_id(Some(POLYGON))
}

/// Signs `vector`'s order through the SDK and returns the signature exactly as
/// it would go on the wire.
async fn sign_vector(vector: &serde_json::Value) -> String {
    let signer = test_signer();
    let sig_type = match vector["order"]["signatureType"].as_u64().expect("sigtype") {
        0 => SignatureType::Eoa,
        3 => SignatureType::Poly1271,
        other => panic!("vector uses unhandled signature type {other}"),
    };
    let funder = match sig_type {
        SignatureType::Eoa => None,
        _ => Some(addr(&vector["deposit_wallet"])),
    };
    let host = neg_risk_stub(vector["neg_risk"].as_bool().expect("neg_risk")).await;
    let client = signing_client(&host, &signer, sig_type, funder).await;
    let signed = client
        .sign(&signer, signable_from(vector))
        .await
        .expect("sign");
    signed.signature.to_string()
}

#[tokio::test]
async fn eoa_order_signature_matches_the_python_reference() {
    // Control: proves the harness (order fields, domain, exchange contract)
    // reproduces the reference on the shape we already trade in production,
    // so a type-3 failure means type 3 and not the scaffolding.
    let v = case("eoa_buy");
    assert_eq!(
        sign_vector(&v).await,
        v["signature"].as_str().expect("frozen signature"),
        "type-0 signature drifted from py-clob-client-v2"
    );
}

#[tokio::test]
async fn deposit_wallet_order_signature_matches_the_python_reference() {
    let v = case("poly1271_buy");
    assert_eq!(
        sign_vector(&v).await,
        v["signature"].as_str().expect("frozen signature"),
        "POLY_1271 wrapped signature drifted from py-clob-client-v2"
    );
}

#[tokio::test]
async fn deposit_wallet_negrisk_sell_signature_matches_the_python_reference() {
    // neg-risk selects a DIFFERENT exchange contract, which changes the app
    // domain separator baked into the wrapper — the one input to type-3
    // signing that arrives from the network rather than the order.
    let v = case("poly1271_sell_negrisk");
    assert_eq!(
        sign_vector(&v).await,
        v["signature"].as_str().expect("frozen signature"),
        "POLY_1271 neg-risk signature drifted from py-clob-client-v2"
    );
}

#[tokio::test]
async fn wrapped_signature_carries_the_reference_domain_and_contents_hashes() {
    // Layout the deposit wallet re-derives on-chain:
    //   0x | 65-byte ECDSA | appDomainSeparator | contentsHash | typeString | len
    // Asserted separately so a break names the field, not just "bytes differ".
    for name in ["poly1271_buy", "poly1271_sell_negrisk"] {
        let v = case(name);
        let sig = sign_vector(&v).await;
        let body = sig.strip_prefix("0x").expect("0x-prefixed");

        assert_eq!(
            &body[130..194],
            v["app_domain_separator"].as_str().expect("frozen").trim_start_matches("0x"),
            "{name}: app domain separator"
        );
        assert_eq!(
            &body[194..258],
            v["contents_hash"].as_str().expect("frozen").trim_start_matches("0x"),
            "{name}: contents hash"
        );

        let type_string = vectors()["order_type_string"]
            .as_str()
            .expect("frozen type string")
            .to_string();
        let type_hex = hex::encode(type_string.as_bytes());
        assert_eq!(&body[258..258 + type_hex.len()], type_hex, "{name}: contents type string");
        assert_eq!(
            &body[258 + type_hex.len()..],
            format!("{:04x}", type_string.len()),
            "{name}: contents type length suffix"
        );
    }
}

#[tokio::test]
async fn wrapped_inner_signature_is_over_the_reference_erc7739_digest() {
    // The digest itself never reaches the wire, so pin it by recovery: the
    // inner ECDSA signature must recover the owner EOA against the frozen
    // digest. A different digest cannot recover the same address.
    for name in ["poly1271_buy", "poly1271_sell_negrisk"] {
        let v = case(name);
        let sig = sign_vector(&v).await;
        let inner = Signature::from_str(&sig[..132]).expect("inner ecdsa signature");
        let digest = b256(&v["digest"]);
        assert_eq!(
            inner
                .recover_address_from_prehash(&digest)
                .expect("recover"),
            addr(&v["eoa"]),
            "{name}: inner signature is not over the reference digest"
        );
    }
}

#[tokio::test]
async fn deposit_wallet_order_names_the_wallet_as_both_maker_and_signer() {
    // The one order-STRUCT difference vs type 1/2: the `signer` field holds the
    // deposit wallet (it is the EIP-1271 verifying contract), not the EOA. Get
    // this wrong and the contract derives a different domain and rejects.
    let v = case("poly1271_buy");
    let wallet = v["deposit_wallet"].as_str().expect("wallet");
    assert_eq!(v["order"]["maker"], wallet);
    assert_eq!(v["order"]["signer"], wallet);
    assert_ne!(v["order"]["signer"], v["eoa"]);

    // ...whereas the type-0 control keeps the EOA in both.
    let e = case("eoa_buy");
    assert_eq!(e["order"]["maker"], e["eoa"]);
    assert_eq!(e["order"]["signer"], e["eoa"]);
}
