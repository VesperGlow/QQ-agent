FROM golang:1.24-alpine AS build

ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY} CGO_ENABLED=0
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN go test ./... && go build -trimpath -ldflags="-s -w" -o /out/qqbot .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates tzdata && \
    addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app
COPY --from=build /out/qqbot /usr/local/bin/qqbot
USER app
EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/qqbot"]

