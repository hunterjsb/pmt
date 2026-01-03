use lambda_http::{run, tracing, Error};
use pmproxy::{build_router, ProxyState};
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Error> {
    tracing::init_default_subscriber();

    let state = Arc::new(ProxyState::new().map_err(|e| Error::from(e.to_string()))?);
    let app = build_router(state);

    run(app).await
}
